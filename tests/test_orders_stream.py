"""Tests fuer ``broker_gateway.streams.orders.OrdersBroadcaster`` und
den ``/v1/orders/stream``-SSE-Endpoint (AP-11 K6).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_ORDERS_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.cp.topics.sor import SorFrame, SorTopicAdapter
from broker_gateway.main import create_app
from broker_gateway.streams.orders import (
    OrdersBroadcaster,
    OrderStreamEvent,
)


# ---------------------------------------------------------------------------
# OrdersBroadcaster - reine Logik (ohne SSE)
# ---------------------------------------------------------------------------


async def test_broadcaster_emits_bootstrap_event_first() -> None:
    bc = OrdersBroadcaster()
    bootstrap = [
        SorFrame(order_id=1, account="U1", symbol="AAPL", status="accepted")
    ]
    iterator = await bc.subscribe(
        account="U1",
        consumer_id="c1",
        bootstrap=bootstrap,
    )
    event = await asyncio.wait_for(_first(iterator), timeout=1.0)
    assert event.event_type == "bootstrap"
    assert event.payload["orders"][0]["symbol"] == "AAPL"


async def test_publish_after_subscribe_reaches_consumer() -> None:
    bc = OrdersBroadcaster()
    iterator = await bc.subscribe(
        account="U1",
        consumer_id="c1",
        bootstrap=None,
    )
    # Live-Frame nach Subscribe.
    bc.publish(
        "U1",
        SorFrame(order_id=2, account="U1", symbol="MSFT", status="filled"),
    )
    event = await asyncio.wait_for(_first(iterator), timeout=1.0)
    assert event.event_type == "order"
    assert event.payload["order_id"] == 2
    assert event.payload["status"] == "filled"


async def test_last_event_id_skips_already_seen_buffered_events() -> None:
    bc = OrdersBroadcaster()
    bootstrap = [
        SorFrame(order_id=1, account="U1", symbol="AAPL", status="accepted")
    ]
    # Erster Subscribe -> bootstrap-Event (id=0).
    first_iter = await bc.subscribe("U1", "c1", bootstrap=bootstrap)
    bootstrap_event = await asyncio.wait_for(
        _first(first_iter), timeout=1.0
    )
    assert bootstrap_event.event_id == 0
    # Live-Frame -> id=1.
    bc.publish(
        "U1",
        SorFrame(order_id=1, account="U1", symbol="AAPL", status="filled"),
    )

    # Zweiter Subscribe mit last_event_id=0 - der bootstrap-Event ist
    # bereits gesehen, der live-Event nicht. Wir erwarten den Live.
    second_iter = await bc.subscribe(
        "U1", "c2", bootstrap=None, last_event_id=0
    )
    next_event = await asyncio.wait_for(_first(second_iter), timeout=1.0)
    assert next_event.event_id >= 1
    assert next_event.event_type == "order"


async def test_publish_unknown_account_is_silent() -> None:
    bc = OrdersBroadcaster()
    # Ohne Subscriber - der Aufruf darf nicht throw'en.
    bc.publish(
        "Unknown",
        SorFrame(order_id=99, account="Unknown", symbol="AAPL"),
    )
    assert bc.active_accounts == set()


async def _first(iterator) -> OrderStreamEvent:
    async for event in iterator:
        return event
    raise AssertionError("Iterator hat kein Event geliefert")


# ---------------------------------------------------------------------------
# /v1/orders/stream - Scope-403 (ohne Live-Smoke)
# ---------------------------------------------------------------------------


_ADMIN_VALUE = "orders-admin-token-aaaaaaaaaaaaaaaaaa"


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


def test_orders_stream_requires_orders_read_scope(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    no_scope = "orders-no-scope-aaaaaaaaaaaaaaaaaa"
    store.put(
        Token(value=no_scope, caller_id="other", scopes=[])
    )
    response = client.get(
        "/v1/orders/stream?account=U25235077",
        headers={"Authorization": f"Bearer {no_scope}"},
    )
    assert response.status_code == 403
