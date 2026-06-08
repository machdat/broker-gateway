"""API-Tests fuer /v1/instruments/{conid}/historical/* und
/v1/instruments/{conid}/fundamentals (Karte a5c7ff1c).

Mock-Strategie: TWSHistoricalService wird per dependency_overrides
gegen eine MagicMock-Instanz getauscht, sodass wir die HTTP-Schicht
isoliert testen koennen. Das cp-Gateway-Mock bleibt aktiv (default
fixture) - die Endpunkte brauchen require_session_ok und damit ein
healthy AuthLifecycle-Snapshot.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from broker_gateway.api.v1.instruments import get_historical_service
from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_FUNDAMENTALS_READ,
    SCOPE_HISTORICAL_READ,
    SCOPE_INSTRUMENTS_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import InstrumentsService
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app
from broker_gateway.tws.historical import (
    FundamentalReport,
    FundamentalsResponse,
    HistoricalBarsResponse,
    TWSHistoricalService,
)
from broker_gateway.tws.types import Bar


_ADMIN_VALUE = "historical-admin-token-aaaaaaaaaaaaaa"
_HISTORICAL_VALUE = "historical-read-token-bbbbbbbbbbbbb"
_FUNDAMENTALS_VALUE = "fundamentals-read-token-ccccccccccc"
_INSTRUMENTS_ONLY_VALUE = "instruments-only-token-dddddddddddd"


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(
        Token(
            value=_ADMIN_VALUE, caller_id="bootstrap-admin", scopes=[SCOPE_ADMIN_ALL]
        )
    )
    s.put(
        Token(
            value=_HISTORICAL_VALUE,
            caller_id="psm-paper",
            scopes=[SCOPE_HISTORICAL_READ],
        )
    )
    s.put(
        Token(
            value=_FUNDAMENTALS_VALUE,
            caller_id="psm-paper",
            scopes=[SCOPE_FUNDAMENTALS_READ],
        )
    )
    s.put(
        Token(
            value=_INSTRUMENTS_ONLY_VALUE,
            caller_id="psm-paper",
            scopes=[SCOPE_INSTRUMENTS_READ],
        )
    )
    return s


@pytest.fixture
async def cp_client(cp_gateway_mock):
    client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    yield client
    await client.aclose()


@pytest.fixture
async def lifecycle(cp_client: CPGatewayClient) -> AuthLifecycle:
    lc = AuthLifecycle(
        cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0
    )
    yield lc
    await lc.stop()


@pytest.fixture
def instruments(cp_client: CPGatewayClient) -> InstrumentsService:
    return InstrumentsService(cp_client, ttl_seconds=300.0)


@pytest.fixture
def historical_mock() -> MagicMock:
    """MagicMock fuer TWSHistoricalService mit AsyncMock-Methoden."""
    mock = MagicMock(spec=TWSHistoricalService)
    mock.historical_bars = AsyncMock()
    mock.fundamentals = AsyncMock()
    return mock


@pytest.fixture
async def client_with_historical(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    instruments: InstrumentsService,
    historical_mock: MagicMock,
    cp_gateway_mock,
):
    """TestClient mit injiziertem Mock-Historical-Service."""
    application = create_app(
        store=store, lifecycle=lifecycle, instruments_service=instruments
    )
    application.dependency_overrides[get_historical_service] = lambda: historical_mock
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture
async def client_without_historical(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    instruments: InstrumentsService,
    cp_gateway_mock,
):
    """TestClient ohne historical-Override — simuliert cp-Mode."""
    application = create_app(
        store=store, lifecycle=lifecycle, instruments_service=instruments
    )
    with TestClient(application) as test_client:
        yield test_client


def _auth(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def _make_bar_response(what_to_show: str = "TRADES") -> HistoricalBarsResponse:
    return HistoricalBarsResponse(
        conid=265598,
        bar_size="1 day",
        duration="1 Y",
        use_rth=True,
        what_to_show=what_to_show,
        records=[
            Bar(
                timestamp=datetime(2026, 5, 16, tzinfo=UTC),
                open=Decimal("100.0"),
                high=Decimal("110.0"),
                low=Decimal("95.0"),
                close=Decimal("105.0"),
                volume=Decimal("1000"),
            )
        ],
    )


def _make_fundamentals_response() -> FundamentalsResponse:
    return FundamentalsResponse(
        conid=265598,
        records=[
            FundamentalReport(
                report_type="ReportSnapshot",
                xml="<ReportSnapshot>data</ReportSnapshot>",
            )
        ],
    )


# --------------------------------------------------------------------------
# Historical Bars — happy-path und Scope-Pruefung
# --------------------------------------------------------------------------


class TestHistoricalDaily:
    async def test_returns_200_with_admin_token(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.historical_bars.return_value = _make_bar_response()
        r = client_with_historical.get(
            "/v1/instruments/265598/historical/daily", headers=_auth(_ADMIN_VALUE)
        )
        assert r.status_code == 200
        body = r.json()
        assert body["conid"] == 265598
        assert body["bar_size"] == "1 day"
        assert len(body["records"]) == 1

    async def test_returns_200_with_historical_scope(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.historical_bars.return_value = _make_bar_response()
        r = client_with_historical.get(
            "/v1/instruments/265598/historical/daily",
            headers=_auth(_HISTORICAL_VALUE),
        )
        assert r.status_code == 200

    async def test_rejects_instruments_only_token_with_403(
        self,
        client_with_historical: TestClient,
    ) -> None:
        r = client_with_historical.get(
            "/v1/instruments/265598/historical/daily",
            headers=_auth(_INSTRUMENTS_ONLY_VALUE),
        )
        assert r.status_code == 403

    async def test_passes_duration_query_through(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.historical_bars.return_value = _make_bar_response()
        client_with_historical.get(
            "/v1/instruments/265598/historical/daily?duration=6+M&useRTH=false",
            headers=_auth(_HISTORICAL_VALUE),
        )
        call_kwargs = historical_mock.historical_bars.await_args.kwargs
        assert call_kwargs["duration"] == "6 M"
        assert call_kwargs["use_rth"] is False
        assert call_kwargs["bar_size"] == "1 day"


class TestHistoricalWhatToShow:
    async def test_default_what_to_show_is_trades(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.historical_bars.return_value = _make_bar_response()
        r = client_with_historical.get(
            "/v1/instruments/265598/historical/daily",
            headers=_auth(_HISTORICAL_VALUE),
        )
        assert r.status_code == 200
        assert r.json()["what_to_show"] == "TRADES"
        call_kwargs = historical_mock.historical_bars.await_args.kwargs
        assert call_kwargs["what_to_show"] == "TRADES"

    async def test_passes_what_to_show_query_through(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.historical_bars.return_value = _make_bar_response(
            what_to_show="ADJUSTED_LAST"
        )
        r = client_with_historical.get(
            "/v1/instruments/265598/historical/daily?whatToShow=ADJUSTED_LAST",
            headers=_auth(_HISTORICAL_VALUE),
        )
        assert r.status_code == 200
        assert r.json()["what_to_show"] == "ADJUSTED_LAST"
        call_kwargs = historical_mock.historical_bars.await_args.kwargs
        assert call_kwargs["what_to_show"] == "ADJUSTED_LAST"

    async def test_invalid_what_to_show_returns_422(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        r = client_with_historical.get(
            "/v1/instruments/265598/historical/daily?whatToShow=FOO",
            headers=_auth(_HISTORICAL_VALUE),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "unsupported_what_to_show"
        # Bei Validierungsfehler darf der Service gar nicht erst gerufen werden.
        historical_mock.historical_bars.assert_not_awaited()

    async def test_what_to_show_applies_to_all_four_routes(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.historical_bars.return_value = _make_bar_response(
            what_to_show="ADJUSTED_LAST"
        )
        for path in ("daily", "hourly", "15min", "1min"):
            r = client_with_historical.get(
                f"/v1/instruments/265598/historical/{path}?whatToShow=ADJUSTED_LAST",
                headers=_auth(_HISTORICAL_VALUE),
            )
            assert r.status_code == 200, path
            call_kwargs = historical_mock.historical_bars.await_args.kwargs
            assert call_kwargs["what_to_show"] == "ADJUSTED_LAST", path


class TestHistoricalHourly:
    async def test_routes_with_hourly_bar_size(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.historical_bars.return_value = _make_bar_response()
        client_with_historical.get(
            "/v1/instruments/265598/historical/hourly",
            headers=_auth(_HISTORICAL_VALUE),
        )
        call_kwargs = historical_mock.historical_bars.await_args.kwargs
        assert call_kwargs["bar_size"] == "1 hour"
        assert call_kwargs["duration"] == "30 D"


class TestHistorical15Min:
    async def test_routes_with_15min_bar_size(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.historical_bars.return_value = _make_bar_response()
        client_with_historical.get(
            "/v1/instruments/265598/historical/15min",
            headers=_auth(_HISTORICAL_VALUE),
        )
        call_kwargs = historical_mock.historical_bars.await_args.kwargs
        assert call_kwargs["bar_size"] == "15 mins"
        assert call_kwargs["duration"] == "7 D"


class TestHistorical1Min:
    async def test_routes_with_1min_bar_size(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.historical_bars.return_value = _make_bar_response()
        client_with_historical.get(
            "/v1/instruments/265598/historical/1min",
            headers=_auth(_HISTORICAL_VALUE),
        )
        call_kwargs = historical_mock.historical_bars.await_args.kwargs
        assert call_kwargs["bar_size"] == "1 min"
        assert call_kwargs["duration"] == "1 D"


# --------------------------------------------------------------------------
# Fundamentals
# --------------------------------------------------------------------------


class TestFundamentals:
    async def test_returns_200_with_fundamentals_scope(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.fundamentals.return_value = _make_fundamentals_response()
        r = client_with_historical.get(
            "/v1/instruments/265598/fundamentals",
            headers=_auth(_FUNDAMENTALS_VALUE),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["conid"] == 265598
        assert len(body["records"]) == 1
        assert body["records"][0]["report_type"] == "ReportSnapshot"

    async def test_rejects_historical_only_token_with_403(
        self,
        client_with_historical: TestClient,
    ) -> None:
        # historical:read deckt fundamentals nicht ab — saubere Trennung.
        r = client_with_historical.get(
            "/v1/instruments/265598/fundamentals",
            headers=_auth(_HISTORICAL_VALUE),
        )
        assert r.status_code == 403

    async def test_default_report_types_used_when_query_empty(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.fundamentals.return_value = _make_fundamentals_response()
        client_with_historical.get(
            "/v1/instruments/265598/fundamentals",
            headers=_auth(_FUNDAMENTALS_VALUE),
        )
        call_kwargs = historical_mock.fundamentals.await_args.kwargs
        assert call_kwargs["report_types"] == [
            "ReportSnapshot",
            "ReportsFinSummary",
            "ReportsFinStatements",
            "RESC",
        ]

    async def test_custom_report_types_passed_through(
        self,
        client_with_historical: TestClient,
        historical_mock: MagicMock,
    ) -> None:
        historical_mock.fundamentals.return_value = _make_fundamentals_response()
        client_with_historical.get(
            "/v1/instruments/265598/fundamentals?report_types=ReportSnapshot,RESC",
            headers=_auth(_FUNDAMENTALS_VALUE),
        )
        call_kwargs = historical_mock.fundamentals.await_args.kwargs
        assert call_kwargs["report_types"] == ["ReportSnapshot", "RESC"]


# --------------------------------------------------------------------------
# cp-Mode-Fallback: ohne Override → 503
# --------------------------------------------------------------------------


class TestCpModeFallback:
    async def test_historical_returns_503_without_override(
        self,
        client_without_historical: TestClient,
    ) -> None:
        r = client_without_historical.get(
            "/v1/instruments/265598/historical/daily",
            headers=_auth(_ADMIN_VALUE),
        )
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "service_unavailable_in_cp_mode"

    async def test_fundamentals_returns_503_without_override(
        self,
        client_without_historical: TestClient,
    ) -> None:
        r = client_without_historical.get(
            "/v1/instruments/265598/fundamentals",
            headers=_auth(_ADMIN_VALUE),
        )
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "service_unavailable_in_cp_mode"


# --------------------------------------------------------------------------
# Auth-Check ohne Token
# --------------------------------------------------------------------------


class TestAuth:
    async def test_historical_without_token_returns_401(
        self,
        client_with_historical: TestClient,
    ) -> None:
        r = client_with_historical.get("/v1/instruments/265598/historical/daily")
        assert r.status_code == 401

    async def test_fundamentals_without_token_returns_401(
        self,
        client_with_historical: TestClient,
    ) -> None:
        r = client_with_historical.get("/v1/instruments/265598/fundamentals")
        assert r.status_code == 401


# --------------------------------------------------------------------------
# OpenAPI lists all five endpoints (Verification #1 der Karte)
# --------------------------------------------------------------------------


class TestOpenApi:
    async def test_lists_five_new_endpoints(
        self,
        client_with_historical: TestClient,
    ) -> None:
        r = client_with_historical.get("/openapi.json")
        assert r.status_code == 200
        paths = set(r.json()["paths"].keys())
        for endpoint in (
            "/v1/instruments/{conid}/historical/daily",
            "/v1/instruments/{conid}/historical/hourly",
            "/v1/instruments/{conid}/historical/15min",
            "/v1/instruments/{conid}/historical/1min",
            "/v1/instruments/{conid}/fundamentals",
        ):
            assert endpoint in paths, f"OpenAPI fehlt {endpoint}"
