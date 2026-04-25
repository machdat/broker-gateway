"""Tests fuer EventBus + GET /v1/events/stream.

Anmerkung: Live-SSE-Konsum ueber ASGITransport gegen die laufende
Streaming-Response loest im pytest-Event-Loop dieselbe Reentrancy-
Haenge aus wie bei /v1/quotes/stream. Daher pruefen wir hier:

- Die Bus-Mechanik direkt am EventBus-Objekt (Filter, Fan-Out,
  Replay, Last-Event-ID).
- Das SSE-Wireformat am Event.to_sse_payload().
- Die HTTP-Pfade des Endpunkts ohne Body-Konsum (401/403/400).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_EVENTS_READ,
    SCOPE_PORTFOLIO_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore, generate_token_value
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app
from broker_gateway.streams.events import (
    ALL_EVENT_TYPES,
    Event,
    EventBus,
    ExecutionEvent,
    PositionEvent,
    StatusEvent,
    utcnow,
)


_ADMIN_VALUE = "events-admin-token-aaaaaaaaaaaaaaaaa"


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(Token(value=_ADMIN_VALUE, caller_id="bootstrap-admin", scopes=[SCOPE_ADMIN_ALL]))
    return s


@pytest.fixture
async def cp_client(cp_gateway_mock) -> CPGatewayClient:
    client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    yield client
    await client.aclose()


@pytest.fixture
async def lifecycle(cp_client: CPGatewayClient) -> AuthLifecycle:
    lc = AuthLifecycle(cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0)
    yield lc
    await lc.stop()


@pytest.fixture
async def bus() -> EventBus:
    b = EventBus(replay_buffer_size=50)
    yield b
    await b.shutdown()


def _make_execution(order_id: str = "ord-1") -> ExecutionEvent:
    return ExecutionEvent(
        order_id=order_id,
        account_id="U25235077",
        conid=265598,
        symbol="AAPL",
        side="BUY",
        filled_quantity="1",
        avg_fill_price="150.00",
        status="Filled",
        occurred_at=utcnow(),
    )


def _make_position(conid: int = 265598) -> PositionEvent:
    return PositionEvent(
        account_id="U25235077",
        conid=conid,
        symbol="AAPL",
        new_quantity="11",
        change="+1",
        occurred_at=utcnow(),
    )


def _make_status(order_id: str = "ord-1", new_status: str = "Filled") -> StatusEvent:
    return StatusEvent(
        order_id=order_id,
        account_id="U25235077",
        old_status="Submitted",
        new_status=new_status,
        reason=None,
        occurred_at=utcnow(),
    )


async def _drain(iterator, n: int, timeout: float = 1.0) -> list[Event]:
    out: list[Event] = []

    async def collect() -> None:
        async for ev in iterator:
            out.append(ev)
            if len(out) >= n:
                return

    await asyncio.wait_for(collect(), timeout=timeout)
    return out


# ---- EventBus: Publish/Subscribe ----


async def test_event_ids_are_monotonic(bus: EventBus) -> None:
    e1 = await bus.publish_execution(_make_execution())
    e2 = await bus.publish_position(_make_position())
    e3 = await bus.publish_status(_make_status())
    assert e1.event_id == 0
    assert e2.event_id == 1
    assert e3.event_id == 2


async def test_subscribe_receives_all_event_types_by_default(bus: EventBus) -> None:
    iterator = await bus.subscribe(consumer_id="c1")
    await bus.publish_execution(_make_execution())
    await bus.publish_position(_make_position())
    await bus.publish_status(_make_status())
    events = await _drain(iterator, 3)
    assert [e.event_type for e in events] == ["execution", "position", "status"]


async def test_subscribe_filters_by_type(bus: EventBus) -> None:
    iterator = await bus.subscribe(
        consumer_id="exec-only",
        types=frozenset({"execution"}),
    )
    await bus.publish_position(_make_position())
    await bus.publish_execution(_make_execution())
    await bus.publish_position(_make_position())
    events = await _drain(iterator, 1)
    assert len(events) == 1
    assert events[0].event_type == "execution"


async def test_unknown_filter_falls_back_to_all_types(bus: EventBus) -> None:
    # Internal API: leere oder voellig unbekannte Filter sollen nicht zu
    # einem stillen "kein Consumer" werden, sondern auf alle Typen
    # zurueckfallen.
    iterator = await bus.subscribe(
        consumer_id="weird",
        types=frozenset({"unknown-type"}),
    )
    await bus.publish_execution(_make_execution())
    events = await _drain(iterator, 1)
    assert len(events) == 1


async def test_fan_out_to_two_consumers(bus: EventBus) -> None:
    it_a = await bus.subscribe(consumer_id="a")
    it_b = await bus.subscribe(consumer_id="b")
    assert bus.consumer_count == 2

    await bus.publish_execution(_make_execution())

    a, b = await asyncio.gather(_drain(it_a, 1), _drain(it_b, 1))
    assert a[0].event_id == b[0].event_id == 0
    assert a[0].body.order_id == "ord-1"
    assert b[0].body.order_id == "ord-1"


async def test_consumer_count_drops_after_unsubscribe(bus: EventBus) -> None:
    iterator = await bus.subscribe(consumer_id="a")
    await bus.publish_execution(_make_execution())
    await _drain(iterator, 1)
    await iterator.aclose()
    # Kurz warten, damit der finally-Block die Map updated.
    await asyncio.sleep(0)
    assert bus.consumer_count == 0


# ---- Replay / Last-Event-ID ----


async def test_replay_skips_events_at_or_before_last_event_id(bus: EventBus) -> None:
    e1 = await bus.publish_execution(_make_execution(order_id="ord-1"))
    e2 = await bus.publish_status(_make_status(order_id="ord-2", new_status="Submitted"))
    e3 = await bus.publish_position(_make_position(conid=272093))

    iterator = await bus.subscribe(consumer_id="late", last_event_id=e1.event_id)
    events = await _drain(iterator, 2)
    assert [ev.event_id for ev in events] == [e2.event_id, e3.event_id]


async def test_replay_respects_type_filter(bus: EventBus) -> None:
    await bus.publish_execution(_make_execution(order_id="ord-A"))
    await bus.publish_position(_make_position())
    await bus.publish_execution(_make_execution(order_id="ord-B"))

    iterator = await bus.subscribe(
        consumer_id="late",
        types=frozenset({"execution"}),
        last_event_id=None,
    )
    events = await _drain(iterator, 2)
    assert [ev.body.order_id for ev in events] == ["ord-A", "ord-B"]


async def test_buffer_caps_replay(bus: EventBus) -> None:
    # bus replay_buffer_size=50 (siehe Fixture)
    for i in range(60):
        await bus.publish_execution(_make_execution(order_id=f"ord-{i}"))
    iterator = await bus.subscribe(consumer_id="late")
    # Erwartet: nur die letzten 50 Events.
    events = await _drain(iterator, 50)
    assert events[0].body.order_id == "ord-10"
    assert events[-1].body.order_id == "ord-59"


# ---- SSE-Wireformat ----


def test_event_to_sse_payload_format() -> None:
    body = ExecutionEvent(
        order_id="ord-42",
        filled_quantity="0.5",
        avg_fill_price="150.50",
        occurred_at=utcnow(),
    )
    event = Event(event_id=7, event_type="execution", body=body)
    payload = event.to_sse_payload()
    assert "id: 7" in payload
    assert "event: execution" in payload
    assert payload.endswith("\n\n")
    data_line = next(line for line in payload.splitlines() if line.startswith("data: "))
    parsed = json.loads(data_line[len("data: ") :])
    assert parsed["order_id"] == "ord-42"
    assert parsed["filled_quantity"] == "0.5"


def test_all_event_types_constant() -> None:
    assert ALL_EVENT_TYPES == frozenset({"execution", "position", "status"})


# ---- Endpunkt /v1/events/stream ----


async def test_stream_endpoint_without_token_returns_401(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    bus: EventBus,
    cp_gateway_mock,
) -> None:
    application = create_app(store=store, lifecycle=lifecycle, event_bus=bus)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.get("/v1/events/stream")
    assert response.status_code == 401


async def test_stream_endpoint_with_wrong_scope_returns_403(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    bus: EventBus,
    cp_gateway_mock,
) -> None:
    bad = generate_token_value()
    store.put(Token(value=bad, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))
    application = create_app(store=store, lifecycle=lifecycle, event_bus=bus)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.get(
                "/v1/events/stream",
                headers={"Authorization": f"Bearer {bad}"},
            )
    assert response.status_code == 403


async def test_stream_endpoint_with_correct_scope_returns_403_when_missing(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    bus: EventBus,
    cp_gateway_mock,
) -> None:
    # Token mit events:read
    good = generate_token_value()
    store.put(Token(value=good, caller_id="bot", scopes=[SCOPE_EVENTS_READ]))
    application = create_app(store=store, lifecycle=lifecycle, event_bus=bus)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.get(
                "/v1/events/stream",
                params={"types": "execution,unknown"},
                headers={"Authorization": f"Bearer {good}"},
            )
    assert response.status_code == 400
    detail = response.json().get("error", {}).get("message", "")
    assert "Unbekannte Event-Typen" in detail
