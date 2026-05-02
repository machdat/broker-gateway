"""Tests fuer die /v1/exchanges-Endpoints und die Erweiterung von
/v1/instruments/{conid} um exchange_id und calendar_url (AP-11 K4).
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_INSTRUMENTS_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.calendar import CalendarService
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import InstrumentsService
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app


_ADMIN_VALUE = "exchanges-admin-token-aaaaaaaaaaaaaaaaaa"
_BASE_URL = "http://cpgateway:5000/v1/api"


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(
        Token(
            value=_ADMIN_VALUE,
            caller_id="bootstrap-admin",
            scopes=[SCOPE_ADMIN_ALL],
        )
    )
    return s


@pytest.fixture
async def cp_client(cp_gateway_mock):
    client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    yield client
    await client.aclose()


@pytest.fixture
async def lifecycle(cp_client):
    lc = AuthLifecycle(
        cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0
    )
    yield lc
    await lc.stop()


@pytest.fixture
def calendar_service(cp_client) -> CalendarService:
    return CalendarService(cp_client)


@pytest.fixture
def instruments_service(cp_client) -> InstrumentsService:
    return InstrumentsService(cp_client, ttl_seconds=300.0)


@pytest.fixture
async def client(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    instruments_service: InstrumentsService,
    calendar_service: CalendarService,
    cp_gateway_mock,
):
    app = create_app(
        store=store,
        lifecycle=lifecycle,
        instruments_service=instruments_service,
        calendar_service=calendar_service,
    )
    with TestClient(app) as test_client:
        yield test_client


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_VALUE}"}


def _schedule_payload() -> list[dict]:
    return [
        {
            "exchange": "NASDAQ",
            "timeZoneId": "America/New_York",
            "schedules": [
                {
                    "tradingScheduleDate": "20260501",
                    "sessions": [
                        {
                            "prop": "NON_LIQUID",
                            "openingTime": "0400",
                            "closingTime": "0930",
                        },
                        {
                            "prop": "LIQUID",
                            "openingTime": "0930",
                            "closingTime": "1600",
                        },
                        {
                            "prop": "NON_LIQUID",
                            "openingTime": "1600",
                            "closingTime": "2000",
                        },
                    ],
                },
                {
                    "tradingScheduleDate": "20260502",
                    "sessions": [],
                },
            ],
        }
    ]


# ---------------------------------------------------------------------------
# /v1/exchanges
# ---------------------------------------------------------------------------


def test_list_exchanges_starts_empty(client: TestClient) -> None:
    response = client.get("/v1/exchanges", headers=_auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert body == {"exchanges": [], "cached_calendars": 0}


def test_list_exchanges_lists_after_calendar_fetch(
    client: TestClient,
    calendar_service: CalendarService,
) -> None:
    # Cache direkt vorbefuellen - der CP-Gateway-Mock-Router liefert kein
    # /trsrv/secdef/schedule, das ist Aufgabe der Live-Verifikation.
    from datetime import datetime, timezone  # noqa: PLC0415

    from broker_gateway.cp.calendar import (  # noqa: PLC0415
        CalendarDay,
        CalendarSession,
        ExchangeCalendar,
        _CacheEntry,
    )

    sample = ExchangeCalendar(
        exchange_id="NASDAQ",
        time_zone="America/New_York",
        days=[
            CalendarDay(
                date=datetime(2026, 5, 1).date(),
                is_holiday=False,
                sessions=[
                    CalendarSession(
                        type="rth",
                        opens_at=datetime(
                            2026, 5, 1, 9, 30, tzinfo=timezone.utc
                        ),
                        closes_at=datetime(
                            2026, 5, 1, 16, 0, tzinfo=timezone.utc
                        ),
                    )
                ],
            ),
            CalendarDay(
                date=datetime(2026, 5, 2).date(),
                is_holiday=True,
                sessions=[],
            ),
        ],
    )
    calendar_service._cache["NASDAQ"] = _CacheEntry(  # noqa: SLF001
        calendar=sample,
        fetched_at=datetime.now(timezone.utc),
    )

    cal_response = client.get(
        "/v1/exchanges/NASDAQ/calendar?days=2",
        headers=_auth_headers(),
    )
    assert cal_response.status_code == 200
    body = cal_response.json()
    assert body["exchange_id"] == "NASDAQ"
    assert body["time_zone"] == "America/New_York"
    assert len(body["days"]) == 2
    assert body["days"][1]["is_holiday"] is True

    list_response = client.get("/v1/exchanges", headers=_auth_headers())
    assert list_response.status_code == 200
    list_body = list_response.json()
    assert list_body["cached_calendars"] == 1
    assert len(list_body["exchanges"]) == 1
    entry = list_body["exchanges"][0]
    assert entry["exchange_id"] == "NASDAQ"
    assert entry["time_zone"] == "America/New_York"


# ---------------------------------------------------------------------------
# days-Range
# ---------------------------------------------------------------------------


def test_days_param_below_one_yields_422(
    client: TestClient, cp_gateway_mock
) -> None:
    response = client.get(
        "/v1/exchanges/NASDAQ/calendar?days=0",
        headers=_auth_headers(),
    )
    assert response.status_code == 422


def test_days_param_above_fourteen_yields_422(
    client: TestClient, cp_gateway_mock
) -> None:
    response = client.get(
        "/v1/exchanges/NASDAQ/calendar?days=15",
        headers=_auth_headers(),
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Scope-Check
# ---------------------------------------------------------------------------


def test_calendar_endpoint_requires_instruments_scope(
    client: TestClient, store: InMemoryTokenStore, cp_gateway_mock
) -> None:
    # Token ohne SCOPE_INSTRUMENTS_READ.
    no_scope = "exchanges-no-scope-token-bbbbbbbbbbbbb"
    store.put(
        Token(
            value=no_scope,
            caller_id="other-caller",
            scopes=[],  # leerer Scope
        )
    )
    response = client.get(
        "/v1/exchanges/NASDAQ/calendar",
        headers={"Authorization": f"Bearer {no_scope}"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# /v1/instruments/{conid} liefert exchange_id und calendar_url
# ---------------------------------------------------------------------------


def test_instruments_detail_includes_exchange_id_and_calendar_url(
    client: TestClient,
) -> None:
    response = client.get("/v1/instruments/265598", headers=_auth_headers())
    assert response.status_code == 200
    body = response.json()
    # IBKR-SSOT fuer den Mock: ``listingExchange = "NASDAQ.NMS"`` (der
    # Sub-Marktplatz unter NASDAQ). Der Adapter reicht den Wert 1:1
    # durch und baut den calendar_url darauf auf.
    assert body["exchange_id"] == "NASDAQ.NMS"
    assert body["calendar_url"] == "/v1/exchanges/NASDAQ.NMS/calendar"
