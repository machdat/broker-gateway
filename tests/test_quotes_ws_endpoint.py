"""Tests fuer ``/v1/quotes/ws`` (AP-11 K7)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_QUOTES_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app


_ADMIN_VALUE = "quotes-ws-token-aaaaaaaaaaaaaaaaaaaaa"


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


def test_ws_connect_with_authorization_header(client: TestClient) -> None:
    with client.websocket_connect(
        "/v1/quotes/ws?conids=265598",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    ) as ws:
        # Erste empfangene Nachricht ist das initiale Snapshot vom Manager
        # (Polling-Modus liefert nach 1s einen Frame; wir warten kurz).
        raw = ws.receive_text()
        body = json.loads(raw)
        assert "id" in body
        assert "event" in body
        assert "data" in body
        assert body["data"]["conid"] == 265598


def test_ws_connect_with_query_token_fallback(client: TestClient) -> None:
    with client.websocket_connect(
        f"/v1/quotes/ws?conids=265598&token={_ADMIN_VALUE}"
    ) as ws:
        raw = ws.receive_text()
        body = json.loads(raw)
        assert body["data"]["conid"] == 265598


def test_ws_connect_with_sec_websocket_protocol_subprotocol(
    client: TestClient,
) -> None:
    with client.websocket_connect(
        "/v1/quotes/ws?conids=265598",
        subprotocols=[f"bearer.{_ADMIN_VALUE}"],
    ) as ws:
        # Server muss das Subprotokoll echoen.
        assert ws.accepted_subprotocol == f"bearer.{_ADMIN_VALUE}"
        raw = ws.receive_text()
        body = json.loads(raw)
        assert body["data"]["conid"] == 265598


def test_ws_without_token_is_rejected(client: TestClient) -> None:
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/v1/quotes/ws?conids=265598") as ws:
            ws.receive_text()
    assert exc.value.code == 1008


def test_ws_with_wrong_scope_is_rejected(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    from starlette.websockets import WebSocketDisconnect  # noqa: PLC0415

    no_scope = "quotes-ws-no-scope-aaaaaaaaaaaaaa"
    store.put(Token(value=no_scope, caller_id="other", scopes=[]))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/v1/quotes/ws?conids=265598",
            headers={"Authorization": f"Bearer {no_scope}"},
        ) as ws:
            ws.receive_text()
    assert exc.value.code == 1008
