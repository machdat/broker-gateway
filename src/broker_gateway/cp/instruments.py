"""Instruments-Adapter: Symbol-Lookup gegen das interne CP-Gateway.

Mappt CP-Gateway-Antworten auf die public API-Schemas (Section 4 im
v1-draft) und cached die Ergebnisse. CP-Gateway-Calls sind teuer und
ratelimitiert; conid-Mapping ändert sich praktisch nie.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from broker_gateway.cache import TTLCache
from broker_gateway.cp.client import CPGatewayClient


_DEFAULT_TTL_S = 7 * 24 * 60 * 60  # 7 Tage


class Instrument(BaseModel):
    conid: int
    symbol: str
    company_name: str | None = None
    currency: str | None = None
    sec_type: str | None = Field(default=None, description="STK, OPT, FUT, ...")


class InstrumentDetail(Instrument):
    exchange: str | None = None


def _map_search_entry(entry: dict[str, Any]) -> Instrument:
    # Live-Schema (AP-02 #04): secType liegt in sections[0].secType, nicht
    # direkt am Top-Level. seed-Schema hatte secType am Top-Level.
    sec_type = entry.get("secType")
    if sec_type is None:
        sections = entry.get("sections")
        if isinstance(sections, list) and sections:
            first = sections[0]
            if isinstance(first, dict):
                sec_type = first.get("secType")
    return Instrument(
        conid=int(entry["conid"]),
        symbol=str(entry.get("symbol") or "").upper(),
        company_name=entry.get("companyName"),
        currency=entry.get("description"),  # CP-Gateway packt currency in description
        sec_type=sec_type,
    )


def _map_info(payload: dict[str, Any]) -> InstrumentDetail:
    # Live-Schema (AP-02 #04): Symbol heisst "ticker", nicht "symbol".
    # exchange-Default in Live ist "listingExchange", "exchange" listet nur
    # validExchanges-Komma-Liste.
    symbol = payload.get("symbol") or payload.get("ticker") or ""
    exchange = payload.get("listingExchange") or payload.get("exchange")
    return InstrumentDetail(
        conid=int(payload["conid"]),
        symbol=str(symbol).upper(),
        company_name=payload.get("companyName"),
        currency=payload.get("currency"),
        sec_type=payload.get("secType"),
        exchange=exchange,
    )


class InstrumentsService:
    def __init__(
        self,
        client: CPGatewayClient,
        *,
        ttl_seconds: float = _DEFAULT_TTL_S,
    ) -> None:
        self._client = client
        self._search_cache: TTLCache[tuple[str, str | None], list[Instrument]] = TTLCache(ttl_seconds)
        self._info_cache: TTLCache[int, InstrumentDetail] = TTLCache(ttl_seconds)

    @property
    def search_cache(self) -> TTLCache[tuple[str, str | None], list[Instrument]]:
        return self._search_cache

    @property
    def info_cache(self) -> TTLCache[int, InstrumentDetail]:
        return self._info_cache

    async def search(self, symbol: str, exchange: str | None = None) -> list[Instrument]:
        symbol_norm = symbol.strip().upper()
        if not symbol_norm:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="symbol darf nicht leer sein")
        key = (symbol_norm, exchange.upper() if exchange else None)
        cached, hit = self._search_cache.get(key)
        if hit:
            return cached or []

        params: dict[str, Any] = {"symbol": symbol_norm}
        if exchange:
            params["exchange"] = exchange
        response = await self._client.get("/iserver/secdef/search", params=params)
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"CP-Gateway-Fehler bei secdef/search: HTTP {response.status_code}",
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="CP-Gateway lieferte unerwartetes Schema bei secdef/search",
            )
        instruments = [_map_search_entry(entry) for entry in payload if "conid" in entry]
        # IBKR liefert pro Symbol mehrere Listings (verschiedene Exchanges:
        # NASDAQ, TSE, MEXI, EBS, Bond, ...). v1-API gibt nur das primaere
        # STK-Listing zurueck - Mehrfach-Listings sind ein
        # Implementations-Detail des CP-Gateways, kein Vertrag.
        primary_stk = next((i for i in instruments if i.sec_type == "STK"), None)
        if primary_stk is not None:
            instruments = [primary_stk]
        self._search_cache.set(key, instruments)
        return instruments

    async def info(self, conid: int) -> InstrumentDetail:
        cached, hit = self._info_cache.get(conid)
        if hit and cached is not None:
            return cached

        response = await self._client.get("/iserver/secdef/info", params={"conid": conid})
        if response.status_code == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"conid {conid} unbekannt")
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"CP-Gateway-Fehler bei secdef/info: HTTP {response.status_code}",
            )
        payload = response.json()
        if not isinstance(payload, dict) or "conid" not in payload:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="CP-Gateway lieferte unerwartetes Schema bei secdef/info",
            )
        detail = _map_info(payload)
        self._info_cache.set(conid, detail)
        return detail
