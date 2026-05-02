"""Tests fuer ``/v1/orders/ws`` (AP-11 K7)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app


_ADMIN_VALUE = "orders-ws-token-aaaaaaaaaaaaaaaaaaaaa"


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
async def client(store, lifecycle, cp_gateway_mock):
    app = create_app(store=store, lifecycle=lifecycle)
    with TestClient(app) as test_client:
        yield test_client


def test_orders_ws_without_token_is_rejected(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/v1/orders/ws?account=U25235077"
        ) as ws:
            ws.receive_text()
    assert exc.value.code == 1008


def test_orders_ws_with_query_token_passes_auth(
    client: TestClient,
) -> None:
    """Auth via Query-Param wird akzeptiert. Der Bootstrap-Loader laeuft
    danach gegen das CP-Mock - das hat keinen iserver/account/orders-
    Mock-Eintrag und der Server schliesst mit 1011 (internal error).
    Dieser Test prueft also den Auth-Pfad isoliert: kein 1008, sondern
    1011 nach Auth-Erfolg. Der Bootstrap-Pfad ist in den
    OrdersBroadcaster-Tests separat abgedeckt."""
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/v1/orders/ws?account=U25235077&token={_ADMIN_VALUE}"
        ) as ws:
            ws.receive_text()
    # 1008 = Auth-Fehler. 1011 = Auth ok, Bootstrap-Loader-Fehler.
    assert exc.value.code == 1011
