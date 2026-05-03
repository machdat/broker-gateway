"""Quotes-Adapter: Snapshot mit First-Call-Prime und Field-Alias-Mapping.

CP-Gateway-Quirk (Anhang 1.5): der erste Snapshot-Call für eine neue
conid liefert leere Felder (nur `conid` + Updated-Timestamp). Der zweite
Call innerhalb weniger Sekunden hat dann die echten Werte. Dieser Service
absorbiert das nach aussen - Consumer sehen immer Daten, falls die
conid am Markt aktiv ist.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from broker_gateway.availability import Availability, map_availability
from broker_gateway.cp.client import CPGatewayClient


_logger = logging.getLogger(__name__)

# IBKR-Field 7762 liefert das Tagesvolumen als skalierter Integer-String:
# der rohe Wert ist Anzahl gehandelter Aktien × 10^6. Empirie 2026-05-03
# gegen Live-Paper-DUP799747: NVDA 7762=129338122634379 → /1e6 = 129.34M
# Aktien (plausibel); AAPL 7762=80972443774809 → /1e6 = 80.97M Aktien.
# Field 87 ist eine Display-Variante mit "B"-Suffix derselben Zahl und
# wird nicht ausgewertet, weil sie nicht numerisch parsbar ist.
_VOLUME_RAW_SCALE = Decimal(1_000_000)
# Sanity-Bound für den rohen 7762-Wert (vor Division). Werte ausserhalb
# 0..10^16 deuten auf einen IBKR-Drift hin und werden auf null gemappt.
_VOLUME_RAW_MAX = Decimal(10) ** 16


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
        if response.status_code == 429:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "cp_pacing_violation",
                    "message": "CP-Gateway pacing-violation (HTTP 429) bei marketdata/snapshot",
                    "retry_after_s": 30,
                },
                headers={"Retry-After": "30"},
            )
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
        volume=_volume_from_field(entry.get(FIELD_ALIASES["volume"])),
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


def _volume_from_field(value: Any) -> str | None:
    if value is None:
        return None
    try:
        raw = Decimal(str(value))
    except (InvalidOperation, ValueError):
        _logger.warning("CP volume value not numeric: %r", value)
        return None
    if raw < 0 or raw > _VOLUME_RAW_MAX:
        _logger.warning("CP volume value out of range: %r", value)
        return None
    return str(int(raw / _VOLUME_RAW_SCALE))
