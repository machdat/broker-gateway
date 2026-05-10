"""Instruments-Adapter gegen die TWS-Socket-API (ib_async).

Pendant zu :class:`broker_gateway.cp.instruments.InstrumentsService`, aber
gegen die TWS-API. Schema-Identitaet: liefert dieselben Pydantic-Modelle
(``Instrument``, ``InstrumentDetail``), damit der Backend-Switch fuer
HTTP-Konsumenten transparent ist.

Folge-Karte zu 33cb35b1 (TWSLifecycle) und 23a368ee (TWSPortfolioService).
AP `2a203c58-...` Phase 2.

ib_async-Patterns:

- ``IB.reqMatchingSymbolsAsync(symbol)`` → ``list[ContractDescription]``.
  IBKR-Rate-Limit ist 1 req/sec, daher Lock + Sleep im Adapter.
- ``IB.reqContractDetailsAsync(Contract(conId=...))`` → ``list[ContractDetails]``.
- ISIN-Lookup ueber ``Contract(secIdType="ISIN", secId="<isin>", secType="STK")``
  + ``reqContractDetailsAsync``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, status

from broker_gateway.cache import TTLCache
from broker_gateway.cp.instruments import (
    Instrument,
    InstrumentDetail,
    is_valid_isin,
)

if TYPE_CHECKING:
    from broker_gateway.tws.client import TWSClient


logger = logging.getLogger(__name__)


__all__ = [
    "TWSInstrumentsService",
    "Instrument",
    "InstrumentDetail",
    "is_valid_isin",
]


_DEFAULT_TTL_S = 7 * 24 * 60 * 60  # 7 Tage, analog zu cp-Variante
_MATCHING_SYMBOLS_RATE_LIMIT_S = 1.05  # IBKR: 1 req/sec, kleiner Sicherheitsabstand


class TWSInstrumentsService:
    """Read-Pfad fuer Instrument-Lookup via ib_async.

    Liefert dieselben Pydantic-Modelle wie ``cp.instruments.InstrumentsService``,
    damit der Backend-Switch fuer Konsumenten transparent ist. TTL-Cache
    analog zu cp-Variante (Default 7 Tage, conid-Mapping aendert sich
    praktisch nie).
    """

    def __init__(
        self,
        client: TWSClient,
        *,
        ttl_seconds: float = _DEFAULT_TTL_S,
        rate_limit_s: float = _MATCHING_SYMBOLS_RATE_LIMIT_S,
    ) -> None:
        self._client = client
        self._search_cache: TTLCache[
            tuple[str, str | None], list[Instrument]
        ] = TTLCache(ttl_seconds)
        self._isin_cache: TTLCache[
            tuple[str, str | None], list[Instrument]
        ] = TTLCache(ttl_seconds)
        self._info_cache: TTLCache[int, InstrumentDetail] = TTLCache(ttl_seconds)
        # IBKR-seitig 1 req/sec auf reqMatchingSymbols. Lock serialisiert
        # parallele Aufrufe; _last_search_at sorgt dafuer, dass aufeinander-
        # folgende Calls den Mindestabstand einhalten.
        self._search_lock = asyncio.Lock()
        self._rate_limit_s = rate_limit_s
        self._last_search_at: float = 0.0

    # ---- Cache-Properties (Test-Zugriff) -----------------------------

    @property
    def search_cache(
        self,
    ) -> TTLCache[tuple[str, str | None], list[Instrument]]:
        return self._search_cache

    @property
    def isin_cache(
        self,
    ) -> TTLCache[tuple[str, str | None], list[Instrument]]:
        return self._isin_cache

    @property
    def info_cache(self) -> TTLCache[int, InstrumentDetail]:
        return self._info_cache

    # ---- Endpunkt-Logik ----------------------------------------------

    async def search(
        self, symbol: str, exchange: str | None = None
    ) -> list[Instrument]:
        symbol_norm = symbol.strip().upper()
        if not symbol_norm:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="symbol darf nicht leer sein",
            )
        exchange_norm = exchange.upper() if exchange else None
        key = (symbol_norm, exchange_norm)
        cached, hit = self._search_cache.get(key)
        if hit:
            return cached or []

        descriptions = await self._matching_symbols(symbol_norm)
        instruments: list[Instrument] = []
        for desc in descriptions:
            contract = getattr(desc, "contract", None)
            if contract is None or not getattr(contract, "conId", None):
                continue
            sec_type = getattr(contract, "secType", None)
            primary_exchange = (
                getattr(contract, "primaryExchange", None)
                or getattr(contract, "exchange", None)
            )
            if exchange_norm and (
                primary_exchange is None
                or primary_exchange.upper() != exchange_norm
            ):
                continue
            instruments.append(
                Instrument(
                    conid=int(contract.conId),
                    symbol=str(getattr(contract, "symbol", "") or "").upper(),
                    company_name=None,  # reqMatchingSymbols liefert keinen Namen
                    currency=getattr(contract, "currency", None),
                    sec_type=sec_type,
                )
            )
        # IBKR liefert pro Symbol mehrere Listings. Konsistent zur cp-Variante
        # nur das primaere STK-Listing zurueckgeben.
        primary_stk = next(
            (i for i in instruments if i.sec_type == "STK"), None
        )
        if primary_stk is not None:
            instruments = [primary_stk]
        self._search_cache.set(key, instruments)
        return instruments

    async def search_by_isin(
        self, isin: str, mic: str | None = None
    ) -> list[Instrument]:
        isin_norm = isin.strip().upper()
        if not is_valid_isin(isin_norm):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "isin muss dem ISO-6166-Format entsprechen "
                    "(12 Zeichen: 2 Land + 9 alphanumerisch + 1 Pruefziffer)"
                ),
            )
        mic_norm = mic.strip().upper() if mic else None
        key = (isin_norm, mic_norm)
        cached, hit = self._isin_cache.get(key)
        if hit:
            return cached or []

        contract_class = await self._import_contract_class()
        probe = contract_class(
            secType="STK",
            secIdType="ISIN",
            secId=isin_norm,
            exchange="SMART",
            currency="",
        )
        details = await self._req_contract_details(probe)
        results: list[Instrument] = []
        for detail in details:
            contract = getattr(detail, "contract", None)
            if contract is None or not getattr(contract, "conId", None):
                continue
            primary_exchange = _detail_exchange_code(detail)
            if mic_norm is not None and primary_exchange != mic_norm:
                continue
            results.append(
                Instrument(
                    conid=int(contract.conId),
                    symbol=str(getattr(contract, "symbol", "") or "").upper(),
                    company_name=getattr(detail, "longName", None),
                    currency=getattr(contract, "currency", None),
                    sec_type=getattr(contract, "secType", None) or "STK",
                    isin=isin_norm,
                )
            )
        self._isin_cache.set(key, results)
        return results

    async def info(self, conid: int) -> InstrumentDetail:
        cached, hit = self._info_cache.get(conid)
        if hit and cached is not None:
            return cached

        contract_class = await self._import_contract_class()
        probe = contract_class(conId=conid, exchange="SMART")
        details = await self._req_contract_details(probe)
        if not details:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"conid {conid} unbekannt",
            )
        first = details[0]
        contract = getattr(first, "contract", None)
        if contract is None:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="ib_async lieferte ContractDetails ohne contract",
            )
        listing_exchange = (
            getattr(contract, "primaryExchange", None)
            or getattr(contract, "exchange", None)
        )
        exchange = listing_exchange
        exchange_id = listing_exchange
        if isinstance(exchange_id, str) and exchange_id:
            exchange_id = exchange_id.upper()
        else:
            exchange_id = None
        calendar_url = (
            f"/v1/exchanges/{exchange_id}/calendar" if exchange_id else None
        )
        result = InstrumentDetail(
            conid=int(contract.conId),
            symbol=str(getattr(contract, "symbol", "") or "").upper(),
            company_name=getattr(first, "longName", None),
            currency=getattr(contract, "currency", None),
            sec_type=getattr(contract, "secType", None),
            exchange=exchange,
            exchange_id=exchange_id,
            calendar_url=calendar_url,
        )
        self._info_cache.set(conid, result)
        return result

    # ---- Helpers -----------------------------------------------------

    async def _matching_symbols(self, symbol_norm: str) -> list[Any]:
        ib = self._client._ib  # noqa: SLF001 - Low-Level-Bruecke
        async with self._search_lock:
            now = time.monotonic()
            wait = self._rate_limit_s - (now - self._last_search_at)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                result = await ib.reqMatchingSymbolsAsync(symbol_norm)
            finally:
                self._last_search_at = time.monotonic()
        return list(result or [])

    async def _req_contract_details(self, contract: Any) -> list[Any]:
        ib = self._client._ib  # noqa: SLF001
        result = await ib.reqContractDetailsAsync(contract)
        return list(result or [])

    async def _import_contract_class(self) -> Any:
        # Dynamischer Import: tests duerfen Contract gegen einen
        # SimpleNamespace-Stub austauschen, ohne ib_async installieren zu
        # muessen (ist als dep da, aber Test-Mocks kommen ohne).
        try:
            from ib_async.contract import Contract  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ib_async ist nicht installiert",
            )
        return Contract


def _detail_exchange_code(detail: Any) -> str | None:
    """Liefert das Heimat-Exchange-Kuerzel aus ContractDetails."""
    contract = getattr(detail, "contract", None)
    if contract is None:
        return None
    primary = (
        getattr(contract, "primaryExchange", None)
        or getattr(contract, "exchange", None)
    )
    if isinstance(primary, str) and primary:
        return primary.upper()
    return None
