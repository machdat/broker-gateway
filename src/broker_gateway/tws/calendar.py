"""Calendar-Adapter fuer das TWS-Backend (Static-Mapping-Strategie).

Pendant zu :class:`broker_gateway.cp.calendar.CalendarService`. ib_async
liefert keinen direkten Calendar-Endpoint und ``ContractDetails.tradingHours``
ist als String-Format aufwaendig zu parsen (siehe Karte
``4de0be6a-...`` Notizen). Statt eines pro-Anfrage-Roundtrips zum IB
Gateway haelt dieser Service ein gepflegtes Static-Mapping in
``data/exchange_calendar.json`` vor und berechnet Calendar-Tage
deterministisch ab dem aktuellen Datum.

Public-API ist 1:1 kompatibel zu ``cp.CalendarService``: ``get(...)``
liefert ein ``ExchangeCalendar``, ``cached_exchanges`` zaehlt die
bekannten Exchanges, ``time_zone_for(...)`` gibt die IANA-Zone fuer
den Exchange-List-Endpoint zurueck. Damit braucht ``api/v1/exchanges.py``
keinen Branch je nach Backend - Duck-Typing wie in den Phasen 1-4 des
HTTP-API-Cutover-AP.

Quelle der Trading-Hours und Holidays: NYSE Hours & Calendar
(https://www.nyse.com/markets/hours-calendars). Aktualisierung jaehrlich
per Folgekarte. Schwesterboersen wie ARCA, AMEX, BATS, IEX, NYSENAT
folgen dem NYSE-Schedule.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status

from broker_gateway.cp.calendar import (
    CalendarDay,
    CalendarSession,
    ExchangeCalendar,
    SessionType,
)


logger = logging.getLogger(__name__)


__all__ = [
    "TWSCalendarService",
    "ExchangeCalendar",
    "CalendarDay",
    "CalendarSession",
    "SessionType",
]


_DEFAULT_DATA_PATH = Path(__file__).parent / "data" / "exchange_calendar.json"
_DEFAULT_DAYS = 14


class TWSCalendarService:
    """Static-Mapping-Calendar-Service fuer das TWS-Backend."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        data_path: Path | None = None,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        self._client = client
        self._data_path = data_path or _DEFAULT_DATA_PATH
        self._today = today_provider or _utc_today
        self._data = _load_data(self._data_path)

    @property
    def cached_exchanges(self) -> list[str]:
        """Liste aller statisch konfigurierten Exchange-IDs (sortiert).

        Im Static-Modus ist die Liste deterministisch nicht-leer - dadurch
        liefert /v1/exchanges nie HTTP 200 mit leerem Body (das war
        die stille Regression im cp-Pfad bei DNS-Fehler auf cpgateway).
        """
        return sorted(self._exchanges_by_id.keys())

    def time_zone_for(self, exchange_id: str) -> str | None:
        """IANA-Zone fuer ``exchange_id`` oder ``None``, wenn nicht
        konfiguriert. Wird vom /v1/exchanges-List-Endpoint genutzt."""
        normalized = exchange_id.strip().upper()
        entry = self._exchanges_by_id.get(normalized)
        return entry["time_zone"] if entry else None

    def description_for(self, exchange_id: str) -> str | None:
        normalized = exchange_id.strip().upper()
        entry = self._exchanges_by_id.get(normalized)
        return entry["description"] if entry else None

    async def get(
        self,
        exchange_id: str,
        *,
        symbol: str | None = None,
    ) -> ExchangeCalendar:
        """Liefert den 14-Tage-Calendar ab heute fuer ``exchange_id``.

        Symbol-Parameter wird ignoriert (Static-Strategie braucht kein
        Probe-Symbol); existiert nur fuer API-Parity mit cp.CalendarService.
        """
        normalized = exchange_id.strip().upper()
        if not normalized:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="exchange_id darf nicht leer sein",
            )
        entry = self._exchanges_by_id.get(normalized)
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "exchange_not_found",
                    "message": (
                        f"exchange_id={normalized!r} ist nicht im "
                        "Static-Calendar-Mapping konfiguriert"
                    ),
                },
            )
        tz = ZoneInfo(entry["time_zone"])
        today = self._today()
        days = [
            self._build_day(today + timedelta(days=offset), tz=tz)
            for offset in range(_DEFAULT_DAYS)
        ]
        return ExchangeCalendar(
            exchange_id=normalized,
            time_zone=entry["time_zone"],
            days=days,
        )

    # ---- Helpers -----------------------------------------------------

    @property
    def _exchanges_by_id(self) -> dict[str, dict[str, str]]:
        return {
            entry["exchange_id"].upper(): entry
            for entry in self._data["exchanges"]
        }

    def _holiday_lookup(self, target: date) -> tuple[bool, bool]:
        """Liefert ``(is_closed, is_early_close)`` fuer ``target``.

        Wochenenden zaehlen als ``is_closed=True`` (Aktienboersen sind
        am Wochenende geschlossen). Andere Tage werden in der
        Holiday-Liste gesucht.
        """
        if target.weekday() >= 5:
            return True, False
        year_key = str(target.year)
        holidays = self._data.get("holidays_us", {}).get(year_key, [])
        target_iso = target.isoformat()
        for entry in holidays:
            if entry.get("date") != target_iso:
                continue
            holiday_type = entry.get("type", "closed")
            if holiday_type == "closed":
                return True, False
            if holiday_type == "early_close_1300":
                return False, True
        return False, False

    def _build_day(self, target: date, *, tz: ZoneInfo) -> CalendarDay:
        is_closed, is_early_close = self._holiday_lookup(target)
        if is_closed:
            return CalendarDay(date=target, is_holiday=True, sessions=[])
        defaults = self._data["default_hours"]
        sessions = list(
            self._build_sessions(
                target,
                tz=tz,
                defaults=defaults,
                is_early_close=is_early_close,
            )
        )
        return CalendarDay(date=target, is_holiday=False, sessions=sessions)

    def _build_sessions(
        self,
        target: date,
        *,
        tz: ZoneInfo,
        defaults: dict[str, dict[str, str]],
        is_early_close: bool,
    ):
        pre = defaults.get("pre_market", {})
        regular = defaults["regular"]
        post = defaults.get("post_market", {})
        early = defaults.get("early_close", {})

        if pre.get("open") and pre.get("close"):
            yield self._make_session(
                "pre",
                target=target,
                tz=tz,
                opens=pre["open"],
                closes=pre["close"],
            )
        rth_close = early["close"] if is_early_close else regular["close"]
        yield self._make_session(
            "rth",
            target=target,
            tz=tz,
            opens=regular["open"],
            closes=rth_close,
        )
        if not is_early_close and post.get("open") and post.get("close"):
            yield self._make_session(
                "post",
                target=target,
                tz=tz,
                opens=post["open"],
                closes=post["close"],
            )

    @staticmethod
    def _make_session(
        session_type: SessionType,
        *,
        target: date,
        tz: ZoneInfo,
        opens: str,
        closes: str,
    ) -> CalendarSession:
        return CalendarSession(
            type=session_type,
            opens_at=_combine(target, opens, tz=tz),
            closes_at=_combine(target, closes, tz=tz),
        )


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _combine(target: date, hhmm: str, *, tz: ZoneInfo) -> datetime:
    parsed = _parse_hhmm(hhmm)
    return datetime.combine(target, parsed, tzinfo=tz)


def _parse_hhmm(value: str) -> time:
    text = value.strip().replace(":", "")
    if len(text) == 3:
        text = "0" + text
    if len(text) != 4 or not text.isdigit():
        raise ValueError(f"ungueltige HHMM-Zeit: {value!r}")
    hour = int(text[:2])
    minute = int(text[2:])
    if hour == 24 and minute == 0:
        return time(23, 59, 59, 999_000)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"ungueltige HHMM-Zeit: {value!r}")
    return time(hour, minute)


def _load_data(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(
            f"Calendar-Datendatei nicht gefunden: {path}"
        )
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Calendar-Datendatei {path} muss ein Objekt sein")
    if not isinstance(data.get("exchanges"), list) or not data["exchanges"]:
        raise ValueError(
            f"Calendar-Datendatei {path} hat keine 'exchanges'-Liste"
        )
    if not isinstance(data.get("default_hours"), dict):
        raise ValueError(
            f"Calendar-Datendatei {path} hat kein 'default_hours'-Objekt"
        )
    return data
