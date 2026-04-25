"""Quotes-Adapter: Snapshot mit First-Call-Prime und Field-Alias-Mapping.

CP-Gateway-Quirk (Anhang 1.5): der erste Snapshot-Call für eine neue
conid liefert leere Felder (nur `conid` + Updated-Timestamp). Der zweite
Call innerhalb weniger Sekunden hat dann die echten Werte. Dieser Service
absorbiert das nach aussen - Consumer sehen immer Daten, falls die
conid am Markt aktiv ist.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Iterable

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from broker_gateway.availability import Availability, map_availability
from broker_gateway.cp.client import CPGatewayClient


# Public-API-Feldname -> IBKR-CP-Gateway-Code.
# Single Source of Truth: alle Feldnamen, die der API-Endpunkt akzeptiert,
# müssen hier eingetragen sein.
FIELD_ALIASES: dict[str, str] = {
    "last": "31",
    "bid": "84",
    "ask": "86",
    "volume": "7762",
    "change_pct": "83",
    "high": "70",
    "low": "71",
    "availability": "6509",
}

_AVAILABILITY_CODE = "6509"

# Default-Felder, wenn der Client `fields` nicht angibt. Availability ist
# immer dabei, damit Consumer ohne Sonderlogik wissen, ob die Daten
# realtime/delayed/frozen sind.
_DEFAULT_FIELDS: tuple[str, ...] = ("last", "bid", "ask", "availability")

_PRIME_DELAY_S = 0.3
_DEFAULT_TIMEOUT_S = 10


class Quote(BaseModel):
    conid: int
    last: str | None = None
    bid: str | None = None
    ask: str | None = None
    volume: str | None = None
    change_pct: str | None = None
    high: str | None = None
    low: str | None = None
    availability: Availability | None = Field(
        default=None,
        description="realtime / delayed / frozen, abgeleitet aus 6509",
    )
    availability_raw: str | None = Field(
        default=None,
        description="Originaler IBKR-6509-Code (z.B. DPB)",
    )
    updated_at: datetime | None = None


def resolve_fields(field_names: Iterable[str]) -> list[str]:
    """Map public-Feldnamen → CP-Gateway-Codes. 422 bei unbekanntem Alias."""
    resolved: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for name in field_names:
        key = name.strip().lower()
        if not key:
            continue
        code = FIELD_ALIASES.get(key)
        if code is None:
            unknown.append(name)
            continue
        if code not in seen:
            seen.add(code)
            resolved.append(code)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unbekannte fields: {sorted(set(unknown))}. Erlaubt: {sorted(FIELD_ALIASES)}",
        )
    return resolved


def normalize_default_fields() -> tuple[str, ...]:
    return _DEFAULT_FIELDS


class QuotesService:
    def __init__(
        self,
        client: CPGatewayClient,
        *,
        prime_delay_s: float = _PRIME_DELAY_S,
    ) -> None:
        self._client = client
        self._prime_delay_s = prime_delay_s

    async def snapshot_with_prime(
        self,
        conids: list[int],
        fields: list[str],
    ) -> list[Quote]:
        """Zwei sequenzielle Snapshot-Calls; nur der zweite zählt nach aussen.

        Rationale: der erste Call subscribet beim CP-Gateway intern; die
        echten Werte kommen erst beim zweiten Aufruf, sobald IBKR die Daten
        gestreamt hat. Wir erzwingen das hier, damit Caller nicht selbst
        retryen muss.
        """
        if not conids:
            return []
        codes = list(fields)
        if _AVAILABILITY_CODE not in codes:
            codes.append(_AVAILABILITY_CODE)
        params = {
            "conids": ",".join(str(c) for c in conids),
            "fields": ",".join(codes),
        }

        # First call: primt das CP-Gateway-internal Subscription-Set.
        await self._call_snapshot(params)
        await asyncio.sleep(self._prime_delay_s)
        # Second call: das ist der, dessen Werte wir an den Caller geben.
        payload = await self._call_snapshot(params)

        return [_quote_from_entry(entry) for entry in payload if "conid" in entry]

    async def _call_snapshot(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        response = await self._client.get("/iserver/marketdata/snapshot", params=params)
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"CP-Gateway-Fehler bei marketdata/snapshot: HTTP {response.status_code}",
            )
        body = response.json()
        if not isinstance(body, list):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="CP-Gateway lieferte unerwartetes Schema bei marketdata/snapshot",
            )
        return body


def _quote_from_entry(entry: dict[str, Any]) -> Quote:
    raw_availability = entry.get(_AVAILABILITY_CODE)
    if raw_availability is not None:
        raw_availability = str(raw_availability)
    updated_at = None
    raw_updated = entry.get("_updated")
    if isinstance(raw_updated, (int, float)):
        # IBKR liefert _updated als Millisekunden-Timestamp.
        updated_at = datetime.fromtimestamp(raw_updated / 1000.0, tz=timezone.utc)

    return Quote(
        conid=int(entry["conid"]),
        last=_str_or_none(entry.get(FIELD_ALIASES["last"])),
        bid=_str_or_none(entry.get(FIELD_ALIASES["bid"])),
        ask=_str_or_none(entry.get(FIELD_ALIASES["ask"])),
        volume=_str_or_none(entry.get(FIELD_ALIASES["volume"])),
        change_pct=_str_or_none(entry.get(FIELD_ALIASES["change_pct"])),
        high=_str_or_none(entry.get(FIELD_ALIASES["high"])),
        low=_str_or_none(entry.get(FIELD_ALIASES["low"])),
        availability=map_availability(raw_availability),
        availability_raw=raw_availability,
        updated_at=updated_at,
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
