"""Tests fuer ``broker_gateway.cp.tradeability.derive_tradeability``.

Wahrheits-Tabelle aus K6-Sektion 5.2:

| current_session | availability_code Prefix | Erwartung   |
|-----------------|--------------------------|-------------|
| rth             | R                        | (true, rth) |
| pre             | R                        | (true, pre) |
| post            | D                        | (true, post)|
| -               | H                        | (false, halted)|
| -               | Z                        | (false, halted)|
| Feiertag        | egal                     | (false, closed)|
| ausserhalb      | R                        | (false, closed)|
| Halbtag         | R nach RTH-Ende          | (false, closed)|
"""
from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from broker_gateway.cp.calendar import (
    CalendarDay,
    CalendarSession,
    ExchangeCalendar,
)
from broker_gateway.cp.tradeability import derive_tradeability


_TZ = ZoneInfo("America/New_York")
_TODAY = date_cls(2026, 5, 1)


def _calendar(*, sessions: list[CalendarSession], holiday: bool = False) -> ExchangeCalendar:
    day = CalendarDay(date=_TODAY, is_holiday=holiday, sessions=sessions)
    return ExchangeCalendar(
        exchange_id="NASDAQ",
        time_zone="America/New_York",
        days=[day],
    )


def _full_day_sessions() -> list[CalendarSession]:
    return [
        CalendarSession(
            type="pre",
            opens_at=datetime.combine(_TODAY, time(4, 0), tzinfo=_TZ),
            closes_at=datetime.combine(_TODAY, time(9, 30), tzinfo=_TZ),
        ),
        CalendarSession(
            type="rth",
            opens_at=datetime.combine(_TODAY, time(9, 30), tzinfo=_TZ),
            closes_at=datetime.combine(_TODAY, time(16, 0), tzinfo=_TZ),
        ),
        CalendarSession(
            type="post",
            opens_at=datetime.combine(_TODAY, time(16, 0), tzinfo=_TZ),
            closes_at=datetime.combine(_TODAY, time(20, 0), tzinfo=_TZ),
        ),
    ]


def _utc(hour: int, minute: int = 0) -> datetime:
    """Hilfsfunktion: New-York-Stunde -> UTC.

    EDT ist UTC-4 (Sommerzeit), unsere Tests laufen alle am 1. Mai 2026
    -> EDT. Statt manueller Offset-Rechnung gehen wir ueber den
    Lokalzeit-Helper.
    """
    local = datetime.combine(_TODAY, time(hour, minute), tzinfo=_TZ)
    return local.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# 1. RTH-Stunde + R Prefix -> (true, rth)
# ---------------------------------------------------------------------------


def test_rth_hour_with_realtime_code_yields_tradeable_rth() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    tradeable, session = derive_tradeability(_utc(11, 0), cal, "RPB")
    assert tradeable is True
    assert session == "rth"


def test_pre_hour_with_realtime_code_yields_tradeable_pre() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    tradeable, session = derive_tradeability(_utc(7, 0), cal, "RPB")
    assert tradeable is True
    assert session == "pre"


def test_post_hour_with_delayed_code_yields_tradeable_post() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    tradeable, session = derive_tradeability(_utc(17, 0), cal, "DPB")
    assert tradeable is True
    assert session == "post"


# ---------------------------------------------------------------------------
# 2. Halted-Codes ueberstimmen den Schedule
# ---------------------------------------------------------------------------


def test_halted_code_in_rth_yields_false_halted() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    tradeable, session = derive_tradeability(_utc(11, 0), cal, "HXX")
    assert tradeable is False
    assert session == "halted"


def test_zero_volume_halt_code_yields_false_halted() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    tradeable, session = derive_tradeability(_utc(11, 0), cal, "ZB")
    assert tradeable is False
    assert session == "halted"


def test_frozen_delayed_code_yields_false_halted() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    tradeable, session = derive_tradeability(_utc(11, 0), cal, "YPB")
    assert tradeable is False
    assert session == "halted"


# ---------------------------------------------------------------------------
# 3. Schedule-Regeln (Feiertag, ausserhalb, Halbtag)
# ---------------------------------------------------------------------------


def test_holiday_yields_false_closed() -> None:
    cal = _calendar(sessions=[], holiday=True)
    tradeable, session = derive_tradeability(_utc(11, 0), cal, "RPB")
    assert tradeable is False
    assert session == "closed"


def test_outside_session_yields_false_closed() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    tradeable, session = derive_tradeability(_utc(2, 0), cal, "RPB")
    assert tradeable is False
    assert session == "closed"


def test_after_half_day_close_yields_false_closed() -> None:
    half_day_sessions = [
        CalendarSession(
            type="rth",
            opens_at=datetime.combine(_TODAY, time(9, 30), tzinfo=_TZ),
            closes_at=datetime.combine(_TODAY, time(13, 0), tzinfo=_TZ),
        ),
    ]
    cal = _calendar(sessions=half_day_sessions)
    # Vor Halbtages-Ende: handelbar.
    early = derive_tradeability(_utc(11, 0), cal, "RPB")
    # Nach Halbtages-Ende: closed.
    late = derive_tradeability(_utc(14, 0), cal, "RPB")
    assert early == (True, "rth")
    assert late == (False, "closed")


# ---------------------------------------------------------------------------
# 4. Edge-Cases
# ---------------------------------------------------------------------------


def test_unknown_availability_code_falls_back_to_closed() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    tradeable, session = derive_tradeability(_utc(11, 0), cal, "XXX")
    assert tradeable is False
    assert session == "closed"


def test_empty_availability_code_falls_back_to_closed_in_rth() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    tradeable, session = derive_tradeability(_utc(11, 0), cal, "")
    assert tradeable is False
    assert session == "closed"


def test_naive_datetime_raises_typeerror() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    with pytest.raises(TypeError):
        derive_tradeability(datetime(2026, 5, 1, 11, 0), cal, "RPB")


def test_lowercase_code_is_normalised() -> None:
    cal = _calendar(sessions=_full_day_sessions())
    tradeable, session = derive_tradeability(_utc(11, 0), cal, "rpb")
    assert tradeable is True
    assert session == "rth"
