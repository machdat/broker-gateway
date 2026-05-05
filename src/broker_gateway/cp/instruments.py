"""Instruments-Adapter: Symbol- und ISIN-Lookup gegen das interne CP-Gateway.

Mappt CP-Gateway-Antworten auf die public API-Schemas (Section 4 in
docs/api/v1.md) und cached die Ergebnisse. CP-Gateway-Calls sind teuer
und ratelimitiert; conid-Mapping ändert sich praktisch nie.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from broker_gateway.cache import TTLCache
from broker_gateway.cp.client import CPGatewayClient


_DEFAULT_TTL_S = 7 * 24 * 60 * 60  # 7 Tage

# ISO 6166: 2 Buchstaben Country-Code + 9 alphanumerische Zeichen + 1 Ziffer Pruefsumme.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def is_valid_isin(value: str) -> bool:
    """Validiert ISIN-Format (12-Zeichen ISO 6166).

    Pruefsummen-Validierung absichtlich nicht hier: IBKR akzeptiert sie selbst,
    und ein Format-Check reicht, um echte Tippfehler/falsche Eingaben zu blocken.
    """
    return bool(_ISIN_RE.match(value))


class Instrument(BaseModel):
    conid: int
    symbol: str
    company_name: str | None = None
    currency: str | None = None
    sec_type: str | None = Field(default=None, description="STK, OPT, FUT, ...")
    isin: str | None = Field(
        default=None,
        description=(
            "ISO 6166 ISIN, falls beim Lookup angegeben oder von IBKR mitgeliefert. "
            "Symbol-Pfad: heute None (CP-Gateway liefert keine ISIN). "
            "ISIN-Pfad: Echo des Query-Werts."
        ),
    )


class InstrumentDetail(Instrument):
    exchange: str | None = None
    exchange_id: str | None = Field(
        default=None,
        description="Heimat-Boerse fuer Schedule-Lookup (listingExchange)",
    )
    calendar_url: str | None = Field(
        default=None,
        description=(
            "Convenience-Link auf /v1/exchanges/{exchange_id}/calendar; "
            "leer, wenn keine listingExchange ableitbar ist."
        ),
    )


def _map_search_entry(entry: dict[str, Any], *, isin: str | None = None) -> Instrument:
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
        isin=isin,
    )


_HEADER_PAREN_RE = re.compile(r"\(([A-Z0-9.]+)\)\s*$")


def _entry_exchange_code(entry: dict[str, Any]) -> str | None:
    """Liefert das Exchange-Kuerzel aus dem CP-Search-Eintrag.

    Schema-Drift zwischen den beiden CP-Pfaden (verifiziert 2026-05-05 live):
    - symbol-Pfad: ``description="NYSE"``, ``companyHeader="SAP SE - IBIS"``
    - ISIN-Pfad (``name=true``): ``description=null``, ``companyHeader="SAP SE (IBIS)"``
    Wir versuchen drei Quellen in dieser Reihenfolge:
      1) ``description`` (symbol-Pfad)
      2) Klammer-Suffix in ``companyHeader`` ``(IBIS)`` (ISIN-Pfad)
      3) Bindestrich-Suffix in ``companyHeader`` ``- IBIS`` (symbol-Pfad-Fallback)
    """
    desc = entry.get("description")
    if isinstance(desc, str) and desc.strip():
        return desc.strip().upper()
    header = entry.get("companyHeader")
    if isinstance(header, str):
        match = _HEADER_PAREN_RE.search(header)
        if match:
            return match.group(1).strip().upper() or None
        if " - " in header:
            return header.rsplit(" - ", 1)[-1].strip().upper() or None
    return None


def _map_info(payload: dict[str, Any]) -> InstrumentDetail:
    # Live-Schema (AP-02 #04): Symbol heisst "ticker", nicht "symbol".
    # exchange-Default in Live ist "listingExchange", "exchange" listet nur
    # validExchanges-Komma-Liste.
    symbol = payload.get("symbol") or payload.get("ticker") or ""
    listing_exchange = payload.get("listingExchange")
    exchange = listing_exchange or payload.get("exchange")
    # ``listingExchange`` ist die Heimat-Boerse fuer den Calendar-Lookup
    # (AP-11 K4 / K6-Anhang C.1.2). Fallback: erster Token aus der
    # ``exchange``-Validlist.
    exchange_id = listing_exchange
    if not exchange_id and isinstance(exchange, str) and exchange:
        exchange_id = exchange.split(",", 1)[0].strip() or None
    if exchange_id:
        exchange_id = exchange_id.upper()
    calendar_url = (
        f"/v1/exchanges/{exchange_id}/calendar" if exchange_id else None
    )
    return InstrumentDetail(
        conid=int(payload["conid"]),
        symbol=str(symbol).upper(),
        company_name=payload.get("companyName"),
        currency=payload.get("currency"),
        sec_type=payload.get("secType"),
        exchange=exchange,
        exchange_id=exchange_id,
        calendar_url=calendar_url,
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
        self._isin_cache: TTLCache[tuple[str, str | None], list[Instrument]] = TTLCache(ttl_seconds)
        self._info_cache: TTLCache[int, InstrumentDetail] = TTLCache(ttl_seconds)

    @property
    def search_cache(self) -> TTLCache[tuple[str, str | None], list[Instrument]]:
        return self._search_cache

    @property
    def isin_cache(self) -> TTLCache[tuple[str, str | None], list[Instrument]]:
        return self._isin_cache

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

    async def search_by_isin(
        self, isin: str, mic: str | None = None
    ) -> list[Instrument]:
        """ISIN-Lookup gegen IBKR. Liefert STK-Cross-Listings als Liste.

        Reihenfolge der CP-Antwort wird unveraendert uebernommen (IBKR sortiert
        nach interner Liquiditaets-Heuristik). Optionaler MIC-Filter (ISO 10383,
        z.B. "XETR", "XNYS") wird gegen den Exchange-Code aus
        ``description``/``companyHeader`` angewendet.
        """
        isin_norm = isin.strip().upper()
        if not is_valid_isin(isin_norm):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "isin muss dem ISO-6166-Format entsprechen (12 Zeichen: "
                    "2 Land + 9 alphanumerisch + 1 Pruefziffer)"
                ),
            )
        mic_norm = mic.strip().upper() if mic else None
        key = (isin_norm, mic_norm)
        cached, hit = self._isin_cache.get(key)
        if hit:
            return cached or []

        # IBKR-CP-API: name=true signalisiert, dass `symbol` als Such-String
        # (Name oder ISIN) statt als Ticker zu interpretieren ist. secType=STK
        # filtert auf Aktien-Listings; weitere SecTypes per Folge-Karten, falls
        # noetig.
        params: dict[str, Any] = {
            "symbol": isin_norm,
            "name": "true",
            "secType": "STK",
        }
        response = await self._client.get("/iserver/secdef/search", params=params)
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"CP-Gateway-Fehler bei secdef/search (ISIN): HTTP "
                    f"{response.status_code}"
                ),
            )
        payload = response.json()
        # Live-Befund 2026-05-05: bei unbekannter ISIN liefert IBKR-CP einen
        # Error-Wrapper {"error": "No contracts found"} statt einer leeren
        # Liste. Der Karten-Vertrag verlangt fuer den ISIN-Pfad explizit
        # leeres Array + HTTP 200, also normalisieren wir den No-Match-Fall.
        if isinstance(payload, dict) and payload.get("error"):
            self._isin_cache.set(key, [])
            return []
        if not isinstance(payload, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="CP-Gateway lieferte unerwartetes Schema bei secdef/search (ISIN)",
            )

        results: list[Instrument] = []
        for entry in payload:
            if "conid" not in entry:
                continue
            if mic_norm is not None:
                exch = _entry_exchange_code(entry)
                if exch != mic_norm:
                    continue
            results.append(_map_search_entry(entry, isin=isin_norm))

        self._isin_cache.set(key, results)
        return results

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
