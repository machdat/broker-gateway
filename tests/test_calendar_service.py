"""Tests fuer ``broker_gateway.cp.calendar.CalendarService``.

Pruefen:

1. Cache-Miss: erster get() ruft den CP-Gateway, zweiter Aufruf
   bedient sich aus dem Cache (kein zweiter HTTP-Call).
2. TTL-Ablauf: nach Ablauf wird der Endpoint erneut gefragt.
3. LIQUID-Mapping zu rth, NON_LIQUID zu pre/post anhand RTH-Position.
4. Halbtages-Session bleibt 1:1 erhalten (kuerzere closingTime).
5. Feiertag wird als is_holiday=true mit leeren sessions abgebildet.
6. cached_exchanges liefert nur nicht-abgelaufene Eintraege.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import respx

from broker_gateway.cp.calendar import CalendarService
from broker_gateway.cp.client import CPGatewayClient


_BASE_URL = "http://cpgateway:5000/v1/api"


def _schedule_response(
    *,
    exchange_id: str = "NASDAQ",
    time_zone: str = "America/New_York",
    days: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        {
            "exchange": exchange_id,
            "timeZoneId": time_zone,
            "schedules": days or [],
        }
    ]


def _full_session_day(date_str: str = "20260501") -> dict[str, Any]:
    return {
        "tradingScheduleDate": date_str,
        "sessions": [
            {"prop": "NON_LIQUID", "openingTime": "0400", "closingTime": "0930"},
            {"prop": "LIQUID", "openingTime": "0930", "closingTime": "1600"},
            {"prop": "NON_LIQUID", "openingTime": "1600", "closingTime": "2000"},
        ],
    }


def _half_day(date_str: str = "20261127") -> dict[str, Any]:
    return {
        "tradingScheduleDate": date_str,
        "sessions": [
            {"prop": "LIQUID", "openingTime": "0930", "closingTime": "1300"},
        ],
    }


def _holiday(date_str: str = "20260525") -> dict[str, Any]:
    return {
        "tradingScheduleDate": date_str,
        "sessions": [],
    }


@pytest.fixture
async def cp_client():
    client = CPGatewayClient(base_url=_BASE_URL)
    yield client
    await client.aclose()


# ---------------------------------------------------------------------------
# 1. Cache-Miss + Cache-Hit
# ---------------------------------------------------------------------------


@respx.mock
async def test_first_get_calls_cp_second_uses_cache(cp_client) -> None:
    route = respx.get(f"{_BASE_URL}/trsrv/secdef/schedule").mock(
        return_value=httpx.Response(
            200,
            json=_schedule_response(days=[_full_session_day()]),
        )
    )

    service = CalendarService(cp_client)

    first = await service.get("NASDAQ")
    second = await service.get("NASDAQ")

    assert first.exchange_id == "NASDAQ"
    assert first.time_zone == "America/New_York"
    assert second.exchange_id == "NASDAQ"
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# 2. TTL-Ablauf
# ---------------------------------------------------------------------------


@respx.mock
async def test_ttl_expiry_triggers_refetch(cp_client) -> None:
    route = respx.get(f"{_BASE_URL}/trsrv/secdef/schedule").mock(
        return_value=httpx.Response(
            200,
            json=_schedule_response(days=[_full_session_day()]),
        )
    )

    fake_now = [datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)]

    def clock() -> datetime:
        return fake_now[0]

    service = CalendarService(cp_client, ttl_s=60.0, clock=clock)

    await service.get("NASDAQ")
    fake_now[0] += timedelta(seconds=61)
    await service.get("NASDAQ")

    assert route.call_count == 2


# ---------------------------------------------------------------------------
# 3. LIQUID/NON_LIQUID-Mapping zu rth/pre/post
# ---------------------------------------------------------------------------


@respx.mock
async def test_session_mapping_liquid_to_rth_and_non_liquid_to_pre_post(
    cp_client,
) -> None:
    respx.get(f"{_BASE_URL}/trsrv/secdef/schedule").mock(
        return_value=httpx.Response(
            200,
            json=_schedule_response(days=[_full_session_day("20260501")]),
        )
    )

    service = CalendarService(cp_client)
    calendar = await service.get("NASDAQ")

    assert len(calendar.days) == 1
    day = calendar.days[0]
    assert day.is_holiday is False
    types = [session.type for session in day.sessions]
    assert types == ["rth", "pre", "post"]

    rth_session = next(s for s in day.sessions if s.type == "rth")
    pre_session = next(s for s in day.sessions if s.type == "pre")
    post_session = next(s for s in day.sessions if s.type == "post")

    assert rth_session.opens_at.hour == 9 and rth_session.opens_at.minute == 30
    assert rth_session.closes_at.hour == 16
    assert pre_session.closes_at <= rth_session.opens_at
    assert post_session.opens_at >= rth_session.closes_at


# ---------------------------------------------------------------------------
# 4. Halbtages-Session
# ---------------------------------------------------------------------------


@respx.mock
async def test_half_day_session_preserves_shorter_close_time(cp_client) -> None:
    respx.get(f"{_BASE_URL}/trsrv/secdef/schedule").mock(
        return_value=httpx.Response(
            200,
            json=_schedule_response(days=[_half_day("20261127")]),
        )
    )

    service = CalendarService(cp_client)
    calendar = await service.get("NASDAQ")
    day = calendar.days[0]

    assert day.is_holiday is False
    assert len(day.sessions) == 1
    rth = day.sessions[0]
    assert rth.type == "rth"
    assert rth.opens_at.hour == 9 and rth.opens_at.minute == 30
    assert rth.closes_at.hour == 13 and rth.closes_at.minute == 0


# ---------------------------------------------------------------------------
# 5. Feiertag
# ---------------------------------------------------------------------------


@respx.mock
async def test_holiday_is_marked_and_sessions_empty(cp_client) -> None:
    respx.get(f"{_BASE_URL}/trsrv/secdef/schedule").mock(
        return_value=httpx.Response(
            200,
            json=_schedule_response(days=[_holiday("20260525")]),
        )
    )

    service = CalendarService(cp_client)
    calendar = await service.get("NASDAQ")
    day = calendar.days[0]

    assert day.is_holiday is True
    assert day.sessions == []


# ---------------------------------------------------------------------------
# 6. cached_exchanges-Property
# ---------------------------------------------------------------------------


@respx.mock
async def test_cached_exchanges_lists_active_entries_only(cp_client) -> None:
    respx.get(f"{_BASE_URL}/trsrv/secdef/schedule").mock(
        return_value=httpx.Response(
            200,
            json=_schedule_response(days=[_full_session_day()]),
        )
    )

    fake_now = [datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)]
    service = CalendarService(
        cp_client, ttl_s=60.0, clock=lambda: fake_now[0]
    )

    await service.get("NASDAQ")
    await service.get("NYSE")
    assert service.cached_exchanges == ["NASDAQ", "NYSE"]

    fake_now[0] += timedelta(seconds=61)
    assert service.cached_exchanges == []


# ---------------------------------------------------------------------------
# Bonus: empty-exchange-id wird abgelehnt
# ---------------------------------------------------------------------------


async def test_empty_exchange_id_is_rejected(cp_client) -> None:
    from fastapi import HTTPException  # noqa: PLC0415

    service = CalendarService(cp_client)
    with pytest.raises(HTTPException) as exc:
        await service.get("   ")
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Bonus: 502 bei kaputtem Schedule-Endpoint
# ---------------------------------------------------------------------------


@respx.mock
async def test_cp_error_surfaces_as_502(cp_client) -> None:
    from fastapi import HTTPException  # noqa: PLC0415

    respx.get(f"{_BASE_URL}/trsrv/secdef/schedule").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    service = CalendarService(cp_client)
    with pytest.raises(HTTPException) as exc:
        await service.get("NASDAQ")
    assert exc.value.status_code == 502


@respx.mock
async def test_live_ibkr_schema_with_tradingtimes_and_lowercase_timezone(
    cp_client,
) -> None:
    """Live-IBKR-Antwort hat ``timezone`` (lowercase) und Sessions in
    ``tradingtimes``, nicht in ``sessions``. Mehrere Boersen-Eintraege
    in der Top-Level-Liste; der Adapter muss den passenden auswaehlen."""
    payload = [
        {
            "id": "p109581",
            "exchange": "RBCCMALP",
            "description": "RBC CMA LLC",
            "timezone": "America/New_York",
            "schedules": [],
        },
        {
            "id": "p1",
            "exchange": "NASDAQ",
            "description": "NASDAQ",
            "timezone": "America/New_York",
            "schedules": [
                {
                    "tradingScheduleDate": "20260501",
                    "sessions": [],
                    "tradingtimes": [
                        {
                            "openingTime": "0930",
                            "closingTime": "1600",
                            "prop": "LIQUID",
                        },
                    ],
                },
                {
                    "tradingScheduleDate": "20260525",
                    "sessions": [],
                    "tradingtimes": [],
                },
            ],
        },
    ]
    respx.get(f"{_BASE_URL}/trsrv/secdef/schedule").mock(
        return_value=httpx.Response(200, json=payload)
    )

    service = CalendarService(cp_client)
    calendar = await service.get("NASDAQ")

    assert calendar.exchange_id == "NASDAQ"
    assert calendar.time_zone == "America/New_York"
    assert len(calendar.days) == 2
    rth_day, holiday = calendar.days
    assert rth_day.is_holiday is False
    assert len(rth_day.sessions) == 1
    assert rth_day.sessions[0].type == "rth"
    assert holiday.is_holiday is True


@respx.mock
async def test_unknown_timezone_surfaces_as_502(cp_client) -> None:
    from fastapi import HTTPException  # noqa: PLC0415

    respx.get(f"{_BASE_URL}/trsrv/secdef/schedule").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "exchange": "NASDAQ",
                    "timeZoneId": "Mars/Olympus_Mons",
                    "schedules": [_full_session_day()],
                }
            ],
        )
    )

    service = CalendarService(cp_client)
    with pytest.raises(HTTPException) as exc:
        await service.get("NASDAQ")
    assert exc.value.status_code == 502
