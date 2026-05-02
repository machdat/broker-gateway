"""Reine Funktion fuer die ``is_tradeable_now`` / ``current_session``-
Ableitung im smd-Frame (AP-11 K5).

Wahrheits-Tabelle aus dem K6-Design (Sektion 5.2):

| current_session  | availability_code Prefix | is_tradeable_now |
|------------------|--------------------------|-------------------|
| rth/pre/post     | R / D                    | true              |
| rth/pre/post     | Z / Y                    | false (halted)    |
| egal             | H...                     | false (halted)    |
| closed           | egal                     | false (closed)    |

Bewusst: keine REST-Calls, kein Logger, kein State. Die Funktion ist
trivial unit-testbar mit erfundenen ``ExchangeCalendar``-Fixtures.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from broker_gateway.cp.calendar import ExchangeCalendar


CurrentSession = Literal["rth", "pre", "post", "closed", "halted"]


_HALTED_PREFIXES: frozenset[str] = frozenset({"H", "Z", "Y"})
_TRADEABLE_PREFIXES: frozenset[str] = frozenset({"R", "D"})


def derive_tradeability(
    now_utc: datetime,
    calendar: ExchangeCalendar,
    availability_code: str | None,
) -> tuple[bool, CurrentSession]:
    """Leitet ``is_tradeable_now`` und ``current_session`` ab.

    ``now_utc`` muss zeitzonenbehaftet (UTC oder kompatibel) sein -
    ein naive datetime wird abgelehnt (TypeError).
    """
    if now_utc.tzinfo is None:
        raise TypeError(
            "derive_tradeability erwartet einen zeitzonenbehafteten datetime"
        )

    prefix = ""
    if availability_code:
        prefix = availability_code.strip().upper()[:1]

    # Halted / Frozen ueberstimmen jeden Schedule-Status.
    if prefix in _HALTED_PREFIXES:
        return False, "halted"

    tz = ZoneInfo(calendar.time_zone)
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()

    matching_day = next(
        (day for day in calendar.days if day.date == today_local),
        None,
    )
    if matching_day is None or matching_day.is_holiday:
        return False, "closed"

    active_session = next(
        (
            session
            for session in matching_day.sessions
            if session.opens_at <= now_local <= session.closes_at
        ),
        None,
    )
    if active_session is None:
        return False, "closed"

    if prefix in _TRADEABLE_PREFIXES:
        # session.type ist bereits ein Literal[rth, pre, post]; die
        # Verschmelzung mit unserem CurrentSession-Literal ist kompatibel.
        return True, active_session.type  # type: ignore[return-value]

    # Unbekannter / leerer Code wird defensiv als closed interpretiert -
    # der Adapter weiss dann, dass keine Trading-Garantie vorliegt.
    return False, "closed"


__all__ = ["CurrentSession", "derive_tradeability"]
