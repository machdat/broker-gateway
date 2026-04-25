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
    return Instrument(
        conid=int(entry["conid"]),
        symbol=str(entry.get("symbol") or "").upper(),
        company_name=entry.get("companyName"),
        currency=entry.get("description"),  # CP-Gateway packt currency in description
        sec_type=entry.get("secType"),
    )


def _map_info(payload: dict[str, Any]) -> InstrumentDetail:
    return InstrumentDetail(
        conid=int(payload["conid"]),
        symbol=str(payload.get("symbol") or "").upper(),
        company_name=payload.get("companyName"),
        currency=payload.get("currency"),
        sec_type=payload.get("secType"),
        exchange=payload.get("exchange"),
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
