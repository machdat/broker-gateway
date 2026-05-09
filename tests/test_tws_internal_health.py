"""Tests fuer GET /v1/internal/tws-health (Karte 441b53db Phase 4)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app
from broker_gateway.tws import ClientIdPool, TWSClient


_BOOTSTRAP_VALUE = "tws-health-admin-token-aaaaaaaaaaaaa"


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(
        Token(
            value=_BOOTSTRAP_VALUE,
            caller_id="bootstrap-admin",
            scopes=[SCOPE_ADMIN_ALL],
        )
    )
    return s


@pytest.fixture
async def cp_client(cp_gateway_mock) -> CPGatewayClient:
    client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    yield client
    await client.aclose()


@pytest.fixture
async def lifecycle(cp_client: CPGatewayClient) -> AuthLifecycle:
    """Echter AuthLifecycle gegen das cp_gateway_mock - reduziert die
    Test-Sichtbarkeit auf das, worum es hier geht: die TWS-Health-Route.
    """
    lc = AuthLifecycle(
        cp_client,
        tickle_interval_s=10.0,  # lang genug, dass der Loop in den Tests nicht stoert
        reauth_max_retries=1,
        reauth_backoff_s=0.0,
        bridge_probe_interval_s=10.0,
        bridge_reauth_warmup_s=0.0,
    )
    yield lc
    await lc.stop()


def _make_tws_client(*, connected: bool = False, paper: bool = True) -> TWSClient:
    """TWSClient mit gemocktem ib_async.IB. ``connected=True`` setzt
    isConnected sofort True, ohne dass connect() aufgerufen werden muss.
    """
    ib = MagicMock()
    ib.connectAsync = AsyncMock(return_value=None)
    ib.disconnect = MagicMock(return_value=None)
    ib.isConnected = MagicMock(return_value=connected)
    return TWSClient(ib=ib, client_id_pool=ClientIdPool(), paper=paper)


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

async def test_tws_health_requires_token(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.get("/v1/internal/tws-health")
        assert response.status_code == 401


async def test_tws_health_returns_503_when_not_configured(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    """Default-Verhalten: kein TWSClient injektiert -> 503 mit
    code=tws_not_configured. Spiegelt den Production-Zustand bis zum
    Cutover (Karte 6)."""
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.get(
            "/v1/internal/tws-health",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "tws_not_configured"


async def test_tws_health_returns_disconnected_state(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    """Mit injektiertem aber un-connectetem TWSClient meldet die Route
    connected=false und client_id=None."""
    tws = _make_tws_client(connected=False, paper=True)
    application = create_app(store=store, lifecycle=lifecycle, tws_client=tws)
    with TestClient(application) as client:
        response = client.get(
            "/v1/internal/tws-health",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is False
    assert body["client_id"] is None
    assert body["paper"] is True
    assert body["read_only"] is True
    assert body["port"] == 4002


async def test_tws_health_returns_connected_state(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    """Nach connect() meldet die Route connected=true und die reservierte
    clientId. Verifiziert die End-to-End-Integration vom Lifecycle ueber
    die Dependency-Override bis zur HTTP-Antwort."""
    tws = _make_tws_client(connected=True, paper=False)
    await tws.connect()
    try:
        application = create_app(store=store, lifecycle=lifecycle, tws_client=tws)
        with TestClient(application) as client:
            response = client.get(
                "/v1/internal/tws-health",
                headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            )
    finally:
        await tws.disconnect()
    assert response.status_code == 200
    body = response.json()
    assert body["connected"] is True
    assert body["client_id"] == 100
    assert body["paper"] is False
    assert body["port"] == 4001
