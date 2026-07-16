"""Tests fuer ``/v1/orders/ws`` (AP-11 K7)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_ORDERS_WRITE,
    SCOPE_QUOTES_READ,
    Token,
)
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


def test_orders_ws_with_write_only_token_passes_auth(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    """Ein Token mit NUR orders:write kommt am Handshake vorbei.

    Scope-Semantik wie GET /v1/orders/{order_id}: das Schreibrecht
    schliesst das Leserecht mit ein (Karte 601c6e09). Vorher verlangte
    der WS-Pfad strikt orders:read und wies denselben Token mit 1008 ab.

    Der Unterschied zwischen 1008 und 1011 ist hier der ganze Test:
    1008 = Auth abgelehnt, 1011 = Auth bestanden und danach der
    Bootstrap-Loader am CP-Mock gescheitert (siehe Test darunter).
    """
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    write_only = "orders-ws-write-only-aaaaaaaaaaaaaa"
    store.put(
        Token(
            value=write_only,
            caller_id="trading-robot-like",
            scopes=[SCOPE_ORDERS_WRITE],
        )
    )

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/v1/orders/ws?account=U25235077&token={write_only}"
        ) as ws:
            ws.receive_text()
    assert exc.value.code == 1011, (
        "write-only-Token wurde am Handshake abgewiesen "
        f"(Close-Code {exc.value.code}, 1008 = Auth-Fehler)"
    )


def test_orders_ws_with_unrelated_scope_is_rejected(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    """Die Weitung ist auf orders:* begrenzt - kein Freifahrtschein."""
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    wrong_scope = "orders-ws-wrong-scope-aaaaaaaaaaaaa"
    store.put(
        Token(
            value=wrong_scope,
            caller_id="quotes-only",
            scopes=[SCOPE_QUOTES_READ],
        )
    )

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            f"/v1/orders/ws?account=U25235077&token={wrong_scope}"
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
