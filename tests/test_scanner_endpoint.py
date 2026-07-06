"""API-Tests fuer GET /v1/scanner und GET /v1/scanner/parameters (RW-07).

Mock-Strategie analog zu test_instruments_historical: TWSScannerService
wird per dependency_overrides gegen eine MagicMock-Instanz getauscht,
sodass die HTTP-Schicht isoliert getestet wird. Das cp-Gateway-Mock
bleibt aktiv (default fixture) - die Endpunkte brauchen require_session_ok
und damit ein healthy AuthLifecycle-Snapshot.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from broker_gateway.api.v1.scanner import get_scanner_service
from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_INSTRUMENTS_READ,
    SCOPE_SCANNER_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import InstrumentsService
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app
from broker_gateway.tws.scanner import (
    ScannerParametersResponse,
    ScannerResultsResponse,
    ScanRow,
    TWSScannerService,
)


_ADMIN_VALUE = "scanner-admin-token-aaaaaaaaaaaaaaa"
_SCANNER_VALUE = "scanner-read-token-bbbbbbbbbbbbbbb"
_INSTRUMENTS_ONLY_VALUE = "scanner-instr-only-token-ccccccccc"


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(
        Token(value=_ADMIN_VALUE, caller_id="bootstrap-admin", scopes=[SCOPE_ADMIN_ALL])
    )
    s.put(
        Token(
            value=_SCANNER_VALUE,
            caller_id="trading-robot",
            scopes=[SCOPE_SCANNER_READ],
        )
    )
    s.put(
        Token(
            value=_INSTRUMENTS_ONLY_VALUE,
            caller_id="trading-robot",
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
def scanner_mock() -> MagicMock:
    """MagicMock fuer TWSScannerService mit AsyncMock-Methoden."""
    mock = MagicMock(spec=TWSScannerService)
    mock.scan = AsyncMock()
    mock.scanner_parameters = AsyncMock()
    return mock


@pytest.fixture
async def client_with_scanner(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    instruments: InstrumentsService,
    scanner_mock: MagicMock,
    cp_gateway_mock,
):
    """TestClient mit injiziertem Mock-Scanner-Service."""
    application = create_app(
        store=store, lifecycle=lifecycle, instruments_service=instruments
    )
    application.dependency_overrides[get_scanner_service] = lambda: scanner_mock
    with TestClient(application) as test_client:
        yield test_client


@pytest.fixture
async def client_without_scanner(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    instruments: InstrumentsService,
    cp_gateway_mock,
):
    """TestClient ohne scanner-Override - simuliert cp-Mode."""
    application = create_app(
        store=store, lifecycle=lifecycle, instruments_service=instruments
    )
    with TestClient(application) as test_client:
        yield test_client


def _auth(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def _make_scan_response() -> ScannerResultsResponse:
    return ScannerResultsResponse(
        scan_code="TOP_PERC_GAIN",
        instrument="STK",
        location_code="STK.US.MAJOR",
        number_of_rows=50,
        scanned_at=datetime(2026, 7, 6, tzinfo=UTC),
        results=[
            ScanRow(
                rank=0,
                con_id=265598,
                symbol="AAPL",
                sec_type="STK",
                exchange="SMART",
                primary_exchange="NASDAQ",
                currency="USD",
            )
        ],
    )


def _make_parameters_response() -> ScannerParametersResponse:
    return ScannerParametersResponse(xml="<ScanParameterResponse>x</ScanParameterResponse>")


# --------------------------------------------------------------------------
# GET /v1/scanner - happy-path und Scope-Pruefung
# --------------------------------------------------------------------------


class TestScan:
    async def test_returns_200_with_admin_token(
        self, client_with_scanner: TestClient, scanner_mock: MagicMock
    ) -> None:
        scanner_mock.scan.return_value = _make_scan_response()
        r = client_with_scanner.get(
            "/v1/scanner?scan_code=TOP_PERC_GAIN", headers=_auth(_ADMIN_VALUE)
        )
        assert r.status_code == 200
        body = r.json()
        assert body["scan_code"] == "TOP_PERC_GAIN"
        assert len(body["results"]) == 1
        assert body["results"][0]["symbol"] == "AAPL"
        assert body["results"][0]["primary_exchange"] == "NASDAQ"

    async def test_returns_200_with_scanner_scope(
        self, client_with_scanner: TestClient, scanner_mock: MagicMock
    ) -> None:
        scanner_mock.scan.return_value = _make_scan_response()
        r = client_with_scanner.get(
            "/v1/scanner?scan_code=TOP_PERC_GAIN", headers=_auth(_SCANNER_VALUE)
        )
        assert r.status_code == 200

    async def test_rejects_token_without_scanner_scope_403(
        self, client_with_scanner: TestClient
    ) -> None:
        r = client_with_scanner.get(
            "/v1/scanner?scan_code=TOP_PERC_GAIN",
            headers=_auth(_INSTRUMENTS_ONLY_VALUE),
        )
        assert r.status_code == 403

    async def test_without_token_returns_401(
        self, client_with_scanner: TestClient
    ) -> None:
        r = client_with_scanner.get("/v1/scanner?scan_code=TOP_PERC_GAIN")
        assert r.status_code == 401

    async def test_missing_scan_code_returns_422(
        self, client_with_scanner: TestClient
    ) -> None:
        r = client_with_scanner.get("/v1/scanner", headers=_auth(_SCANNER_VALUE))
        assert r.status_code == 422

    async def test_passes_params_through(
        self, client_with_scanner: TestClient, scanner_mock: MagicMock
    ) -> None:
        scanner_mock.scan.return_value = _make_scan_response()
        client_with_scanner.get(
            "/v1/scanner?scan_code=HIGH_DIVIDEND_YIELD&instrument=STK"
            "&location_code=STK.EU&number_of_rows=25",
            headers=_auth(_SCANNER_VALUE),
        )
        kwargs = scanner_mock.scan.await_args.kwargs
        assert kwargs["scan_code"] == "HIGH_DIVIDEND_YIELD"
        assert kwargs["instrument"] == "STK"
        assert kwargs["location_code"] == "STK.EU"
        assert kwargs["number_of_rows"] == 25

    async def test_passes_filter_options_through(
        self, client_with_scanner: TestClient, scanner_mock: MagicMock
    ) -> None:
        scanner_mock.scan.return_value = _make_scan_response()
        client_with_scanner.get(
            "/v1/scanner?scan_code=TOP_PERC_GAIN"
            "&filter=peRatioBelow:20&filter=marketCapAbove1e6:500",
            headers=_auth(_SCANNER_VALUE),
        )
        kwargs = scanner_mock.scan.await_args.kwargs
        assert kwargs["filter_options"] == [
            ("peRatioBelow", "20"),
            ("marketCapAbove1e6", "500"),
        ]

    async def test_invalid_filter_returns_422(
        self, client_with_scanner: TestClient, scanner_mock: MagicMock
    ) -> None:
        r = client_with_scanner.get(
            "/v1/scanner?scan_code=TOP_PERC_GAIN&filter=badformat",
            headers=_auth(_SCANNER_VALUE),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_filter_option"
        scanner_mock.scan.assert_not_awaited()

    async def test_number_of_rows_above_50_returns_422(
        self, client_with_scanner: TestClient, scanner_mock: MagicMock
    ) -> None:
        r = client_with_scanner.get(
            "/v1/scanner?scan_code=TOP_PERC_GAIN&number_of_rows=51",
            headers=_auth(_SCANNER_VALUE),
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_number_of_rows"
        scanner_mock.scan.assert_not_awaited()


# --------------------------------------------------------------------------
# GET /v1/scanner/parameters
# --------------------------------------------------------------------------


class TestScannerParameters:
    async def test_returns_200_with_scanner_scope(
        self, client_with_scanner: TestClient, scanner_mock: MagicMock
    ) -> None:
        scanner_mock.scanner_parameters.return_value = _make_parameters_response()
        r = client_with_scanner.get(
            "/v1/scanner/parameters", headers=_auth(_SCANNER_VALUE)
        )
        assert r.status_code == 200
        assert r.json()["xml"].startswith("<ScanParameterResponse")

    async def test_rejects_without_scope_403(
        self, client_with_scanner: TestClient
    ) -> None:
        r = client_with_scanner.get(
            "/v1/scanner/parameters", headers=_auth(_INSTRUMENTS_ONLY_VALUE)
        )
        assert r.status_code == 403


# --------------------------------------------------------------------------
# cp-Mode-Fallback: ohne Override -> 503
# --------------------------------------------------------------------------


class TestCpModeFallback:
    async def test_scan_returns_503_without_override(
        self, client_without_scanner: TestClient
    ) -> None:
        r = client_without_scanner.get(
            "/v1/scanner?scan_code=TOP_PERC_GAIN", headers=_auth(_ADMIN_VALUE)
        )
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "service_unavailable_in_cp_mode"

    async def test_parameters_returns_503_without_override(
        self, client_without_scanner: TestClient
    ) -> None:
        r = client_without_scanner.get(
            "/v1/scanner/parameters", headers=_auth(_ADMIN_VALUE)
        )
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "service_unavailable_in_cp_mode"


# --------------------------------------------------------------------------
# OpenAPI listet die neuen Endpunkte
# --------------------------------------------------------------------------


class TestOpenApi:
    async def test_lists_scanner_endpoints(
        self, client_with_scanner: TestClient
    ) -> None:
        r = client_with_scanner.get("/openapi.json")
        assert r.status_code == 200
        paths = set(r.json()["paths"].keys())
        assert "/v1/scanner" in paths
        assert "/v1/scanner/parameters" in paths
