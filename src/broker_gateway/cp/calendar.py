"""CalendarService - Boersenkalender pro ``exchange_id`` (12h-Cache).

Holt 14-Tage-Schedules vom CP-Gateway-Endpoint
``/trsrv/secdef/schedule`` und normalisiert IBKR-Quirks auf das
einheitliche ``ExchangeCalendar``-Schema (siehe
``docs/architecture/ws-adapter-design.md`` Anhang C):

- ``LIQUID``-Sessions sind Regular-Trading-Hours (``rth``);
  ``NON_LIQUID`` ist Pre- oder Post-Market - der Adapter unterscheidet
  anhand der zeitlichen Position relativ zur RTH-Session.
- Halbtages-Sessions kommen von IBKR nativ als kuerzere
  ``closingTime`` - der Adapter reicht das 1:1 durch.
- Feiertage = Tag mit leerer ``sessions``-Liste; das Ergebnis
  spiegelt das mit ``is_holiday=true, sessions=[]``.
- Die Time-Zone kommt aus dem Schedule-Response selbst (Feld
  ``timeZoneId``); es gibt keinen separaten Endpoint dafuer.

Cache-Strategie

- Pro ``exchange_id`` ein Eintrag mit TTL 12 h (Schedules aendern
  sich praktisch nie unterjaehrig). Cache-Miss -> CP-REST-Call.
- Reines In-Memory-Cache; ein Service-Restart laedt alles neu.

Kein Disk-Persist, kein expliziter Refresh-Endpoint - der ist eine
Folge-Karte (K6-Sektion 8.2-Mitigation), wenn er gebraucht wird.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from typing import Any, Iterable, Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from broker_gateway.cp.client import CPGatewayClient


logger = logging.getLogger(__name__)


_SCHEDULE_TTL_S = 12 * 60 * 60
_INSTRUMENT_TTL_S = 24 * 60 * 60

# IBKR ``/trsrv/secdef/schedule`` verlangt sowohl ``symbol`` als auch
# ``exchange``; der Schedule selbst gilt boersenweit, das Symbol ist nur
# ein Aufhaenger. Default ``AAPL`` deckt NASDAQ/NYSE/US-Boersen ab. Bei
# anderen Boersen kann der Aufrufer per ``service.get(exchange, symbol=...)``
# einen passenden Default mitgeben.
_DEFAULT_SCHEDULE_SYMBOL = "AAPL"

SessionType = Literal["rth", "pre", "post"]


class CalendarSession(BaseModel):
    type: SessionType
    opens_at: datetime
    closes_at: datetime


class CalendarDay(BaseModel):
    date: date_cls
    is_holiday: bool
    sessions: list[CalendarSession] = Field(default_factory=list)


class ExchangeCalendar(BaseModel):
    exchange_id: str
    time_zone: str
    days: list[CalendarDay]


@dataclass
class _CacheEntry:
    calendar: ExchangeCalendar
    fetched_at: datetime


class CalendarService:
    def __init__(
        self,
        client: CPGatewayClient,
        *,
        ttl_s: float = _SCHEDULE_TTL_S,
        clock: "callable" = None,  # type: ignore[assignment]
    ) -> None:
        self._client = client
        self._ttl_s = ttl_s
        self._clock = clock or _utcnow
        self._cache: dict[str, _CacheEntry] = {}

    async def get(
        self,
        exchange_id: str,
        *,
        symbol: str = _DEFAULT_SCHEDULE_SYMBOL,
    ) -> ExchangeCalendar:
        """Liefert den Schedule fuer ``exchange_id`` aus dem Cache oder
        zieht ihn vom CP-Gateway nach.

        IBKR ``/trsrv/secdef/schedule`` verlangt sowohl ``symbol`` als
        auch ``exchange``; der Schedule gilt boersenweit, das Symbol ist
        nur ein Aufhaenger. Default ``AAPL`` (US-Aktie, NASDAQ-Listing)
        deckt die haeufigsten US-Boersen ab.
        """
        normalized = exchange_id.strip().upper()
        if not normalized:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="exchange_id darf nicht leer sein",
            )
        entry = self._cache.get(normalized)
        if entry is not None and not self._is_expired(entry):
            return entry.calendar
        calendar = await self._fetch(normalized, symbol=symbol)
        self._cache[normalized] = _CacheEntry(
            calendar=calendar,
            fetched_at=self._clock(),
        )
        return calendar

    @property
    def cached_exchanges(self) -> list[str]:
        """Liefert die Liste aller Exchange-IDs, fuer die ein nicht-
        abgelaufener Cache-Eintrag existiert (sortiert)."""
        live = [
            ex
            for ex, entry in self._cache.items()
            if not self._is_expired(entry)
        ]
        return sorted(live)

    def _is_expired(self, entry: _CacheEntry) -> bool:
        delta = (self._clock() - entry.fetched_at).total_seconds()
        return delta >= self._ttl_s

    async def _fetch(
        self, exchange_id: str, *, symbol: str
    ) -> ExchangeCalendar:
        params = {
            "assetClass": "STK",
            "symbol": symbol,
            "exchange": exchange_id,
        }
        response = await self._client.get(
            "/trsrv/secdef/schedule",
            params=params,
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    f"CP-Gateway-Fehler bei trsrv/secdef/schedule: "
                    f"HTTP {response.status_code}"
                ),
            )
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="trsrv/secdef/schedule lieferte unerwartetes Schema",
            )
        # IBKR liefert ein Listen-Element pro Boerse; bei einer einzelnen
        # exchange_id kommt eine 1-elementige Liste.
        primary = payload[0]
        if not isinstance(primary, dict):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="trsrv/secdef/schedule lieferte unerwartetes Schema",
            )
        return _normalise(primary, fallback_exchange=exchange_id)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalise(
    payload: dict[str, Any],
    *,
    fallback_exchange: str,
) -> ExchangeCalendar:
    time_zone_id = payload.get("timeZoneId") or payload.get("timeZone")
    if not time_zone_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="trsrv/secdef/schedule lieferte keine timeZoneId",
        )
    try:
        tz = ZoneInfo(time_zone_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"timeZoneId={time_zone_id!r} ist keine bekannte Zone",
        ) from exc

    raw_days_holder = (
        payload.get("schedules") or payload.get("tradingScheduleList") or []
    )
    days: list[CalendarDay] = []
    for raw in raw_days_holder:
        if not isinstance(raw, dict):
            continue
        days.append(_normalise_day(raw, tz=tz))
    days.sort(key=lambda d: d.date)
    exchange_id = (
        payload.get("exchange")
        or payload.get("exchangeId")
        or fallback_exchange
    ).upper()
    return ExchangeCalendar(
        exchange_id=exchange_id,
        time_zone=time_zone_id,
        days=days,
    )


def _normalise_day(raw: dict[str, Any], *, tz: ZoneInfo) -> CalendarDay:
    raw_date = raw.get("tradingScheduleDate") or raw.get("date")
    if not isinstance(raw_date, str):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="schedule-Eintrag ohne tradingScheduleDate",
        )
    parsed_date = _parse_iso_date(raw_date)
    raw_sessions = raw.get("sessions") or []
    sessions = list(_collect_sessions(raw_sessions, day=parsed_date, tz=tz))
    return CalendarDay(
        date=parsed_date,
        is_holiday=not sessions,
        sessions=sessions,
    )


def _collect_sessions(
    raw_sessions: Iterable[Any], *, day: date_cls, tz: ZoneInfo
) -> Iterable[CalendarSession]:
    """Setzt LIQUID/NON_LIQUID-Sessions auf rth/pre/post.

    Algorithmus: zuerst alle LIQUID-Sessions als Kandidaten fuer ``rth``
    sammeln und nach Eroeffnungszeit sortieren. NON_LIQUID-Sessions
    werden anschliessend anhand ihrer Position relativ zur RTH-Session
    auf ``pre`` oder ``post`` gemappt:

    - Schliessen sie vor RTH-Eroeffnung -> ``pre``.
    - Eroeffnen sie nach RTH-Schluss -> ``post``.
    - Andere ueberlappende Faelle werden defensive als ``pre`` markiert
      (sehr selten und nur bei IBKR-Quirks).
    """
    parsed: list[tuple[str, datetime, datetime]] = []
    for entry in raw_sessions:
        if not isinstance(entry, dict):
            continue
        prop = (entry.get("prop") or "").upper()
        opens, closes = _parse_session_window(entry, day=day, tz=tz)
        if opens is None or closes is None:
            continue
        parsed.append((prop, opens, closes))

    rth_window: tuple[datetime, datetime] | None = None
    for prop, opens, closes in parsed:
        if prop == "LIQUID" and rth_window is None:
            rth_window = (opens, closes)
            yield CalendarSession(type="rth", opens_at=opens, closes_at=closes)

    for prop, opens, closes in parsed:
        if prop == "LIQUID":
            continue
        session_type: SessionType = "pre"
        if rth_window is not None:
            rth_open, rth_close = rth_window
            if opens >= rth_close:
                session_type = "post"
            elif closes <= rth_open:
                session_type = "pre"
            else:
                # Ueberlappung mit RTH - sehr selten; defensiver Default
                # ``pre``, weil das die haeufigere Variante ist.
                session_type = "pre"
        yield CalendarSession(type=session_type, opens_at=opens, closes_at=closes)


def _parse_session_window(
    entry: dict[str, Any], *, day: date_cls, tz: ZoneInfo
) -> tuple[datetime | None, datetime | None]:
    opens = _parse_clock_value(entry.get("openingTime"), day=day, tz=tz)
    closes = _parse_clock_value(entry.get("closingTime"), day=day, tz=tz)
    return opens, closes


def _parse_clock_value(
    raw: Any, *, day: date_cls, tz: ZoneInfo
) -> datetime | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    # IBKR liefert ``HHMM`` (z.B. ``"0930"``) oder ``HH:MM``.
    text = text.replace(":", "")
    if not text.isdigit() or len(text) not in (3, 4):
        return None
    if len(text) == 3:
        text = "0" + text
    hour = int(text[:2])
    minute = int(text[2:])
    if hour == 24 and minute == 0:
        # End-of-day: ``2400`` als 23:59:59.999 abbilden.
        return datetime.combine(day, time(23, 59, 59, 999_000), tzinfo=tz)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return datetime.combine(day, time(hour, minute), tzinfo=tz)


def _parse_iso_date(raw: str) -> date_cls:
    text = raw.strip()
    if len(text) == 8 and text.isdigit():
        return date_cls(int(text[:4]), int(text[4:6]), int(text[6:]))
    try:
        return date_cls.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"schedule-Datum {raw!r} unparsebar",
        ) from exc


__all__ = [
    "CalendarDay",
    "CalendarService",
    "CalendarSession",
    "ExchangeCalendar",
    "SessionType",
]
