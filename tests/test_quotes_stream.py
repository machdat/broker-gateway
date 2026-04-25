"""Tests fuer SubscriptionManager + GET /v1/quotes/stream.

Anmerkung: Live-SSE-Konsum (aiter_bytes über ASGITransport gegen die
laufende Streaming-Response) führt im pytest-Event-Loop zu einer
Reentrancy-Hänge zwischen Lifespan-Context, Poll-Task und Reader. Das
echte SSE-Wire-Format wird daher direkt am StreamEvent-Objekt verifiziert
(siehe `test_stream_event_to_sse_payload_format`); die Endpunkt-Tests
prüfen hier nur die HTTP-Pfade ohne Body-Konsum (401/403/429).
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_PORTFOLIO_READ,
    SCOPE_QUOTES_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore, generate_token_value
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.cp.quotes import FIELD_ALIASES
from broker_gateway.main import create_app
from broker_gateway.streams.manager import (
    StreamEvent,
    SubscriptionLimitExceeded,
    SubscriptionManager,
)


_ADMIN_VALUE = "stream-admin-token-aaaaaaaaaaaaaaaaa"


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
async def manager(cp_client: CPGatewayClient) -> SubscriptionManager:
    # Polling-Intervall und Cool-Down kurz halten - Tests sollen schnell sein.
    mgr = SubscriptionManager(
        cp_client,
        poll_interval_s=0.05,
        cool_down_s=0.2,
        max_conids=250,
    )
    yield mgr
    await mgr.shutdown()


async def _drain(iterator, n: int, timeout: float = 2.0) -> list[StreamEvent]:
    out: list[StreamEvent] = []
    async def collect() -> None:
        async for ev in iterator:
            out.append(ev)
            if len(out) >= n:
                return
    await asyncio.wait_for(collect(), timeout=timeout)
    return out


# ---- Manager: Refcount + Fan-Out ----

async def test_manager_active_conid_count_after_first_subscribe(
    manager: SubscriptionManager, cp_gateway_mock
) -> None:
    iterator = await manager.subscribe([265598], [FIELD_ALIASES["last"]], "consumer-a")
    events = await _drain(iterator, 1)
    assert len(events) >= 1
    assert manager.active_conids == {265598}


async def test_two_consumers_share_one_subscription(
    manager: SubscriptionManager, cp_gateway_mock
) -> None:
    it_a = await manager.subscribe([265598], [FIELD_ALIASES["last"]], "consumer-a")
    it_b = await manager.subscribe([265598], [FIELD_ALIASES["last"]], "consumer-b")

    events_a, events_b = await asyncio.gather(_drain(it_a, 2), _drain(it_b, 2))
    assert len(events_a) >= 1
    assert len(events_b) >= 1
    # Genau eine ConidSubscription, beide Consumer hängen daran.
    assert manager.active_conids == {265598}


async def test_disconnect_one_consumer_keeps_subscription(
    manager: SubscriptionManager, cp_gateway_mock
) -> None:
    it_a = await manager.subscribe([265598], [FIELD_ALIASES["last"]], "consumer-a")
    it_b = await manager.subscribe([265598], [FIELD_ALIASES["last"]], "consumer-b")

    await _drain(it_a, 1)
    await _drain(it_b, 1)

    # Consumer A schliesst ab.
    await it_a.aclose()
    # Cool-Down würde nach 0.2 s greifen, aber B hält die Subscription.
    await asyncio.sleep(0.3)
    assert manager.active_conids == {265598}

    await it_b.aclose()


async def test_disconnect_all_consumers_triggers_cool_down_unsubscribe(
    manager: SubscriptionManager, cp_gateway_mock
) -> None:
    iterator = await manager.subscribe([265598], [FIELD_ALIASES["last"]], "consumer-a")
    await _drain(iterator, 1)

    assert 265598 in cp_gateway_mock.subscriptions
    await iterator.aclose()
    # Cool-Down (0.2 s) abwarten + etwas Slack.
    await asyncio.sleep(0.4)
    assert manager.active_conids == set()
    assert 265598 not in cp_gateway_mock.subscriptions


async def test_reattach_within_cool_down_keeps_subscription(
    manager: SubscriptionManager, cp_gateway_mock
) -> None:
    it_a = await manager.subscribe([265598], [FIELD_ALIASES["last"]], "consumer-a")
    await _drain(it_a, 1)
    await it_a.aclose()

    # Vor Cool-Down: neuer Consumer.
    it_b = await manager.subscribe([265598], [FIELD_ALIASES["last"]], "consumer-b")
    await _drain(it_b, 1)

    await asyncio.sleep(0.3)  # ehemaliger Cool-Down wäre vorbei
    assert manager.active_conids == {265598}
    await it_b.aclose()


async def test_subscribe_above_max_raises_limit_exceeded(
    cp_client: CPGatewayClient,
) -> None:
    mgr = SubscriptionManager(cp_client, poll_interval_s=10.0, cool_down_s=0.1, max_conids=2)
    try:
        it_1 = await mgr.subscribe([1001], [FIELD_ALIASES["last"]], "a")
        it_2 = await mgr.subscribe([1002], [FIELD_ALIASES["last"]], "b")
        with pytest.raises(SubscriptionLimitExceeded):
            await mgr.subscribe([1003], [FIELD_ALIASES["last"]], "c")
        await it_1.aclose()
        await it_2.aclose()
    finally:
        await mgr.shutdown()


async def test_replay_buffer_replays_after_last_event_id(
    manager: SubscriptionManager, cp_gateway_mock
) -> None:
    iterator = await manager.subscribe([265598], [FIELD_ALIASES["last"]], "consumer-a")
    initial = await _drain(iterator, 3)
    last_seen = initial[-1].event_id
    await iterator.aclose()

    # Ohne Cool-Down warten - innerhalb des Cool-Down-Fensters wieder verbinden.
    iterator2 = await manager.subscribe(
        [265598],
        [FIELD_ALIASES["last"]],
        "consumer-b",
        last_event_id=last_seen,
    )
    replay = await _drain(iterator2, 1)
    assert all(ev.event_id > last_seen for ev in replay)
    await iterator2.aclose()


# ---- StreamEvent SSE-Format ----

def test_stream_event_to_sse_payload_format() -> None:
    from broker_gateway.cp.quotes import Quote

    quote = Quote(conid=42, last="100.00", bid="99.50", ask="100.50")
    event = StreamEvent(event_id=7, conid=42, quote=quote)
    payload = event.to_sse_payload()
    assert "id: 7" in payload
    assert "event: quote" in payload
    assert payload.endswith("\n\n")
    data_line = next(line for line in payload.splitlines() if line.startswith("data: "))
    parsed = json.loads(data_line[len("data: ") :])
    assert parsed["conid"] == 42
    assert parsed["last"] == "100.00"


# ---- Endpunkt /v1/quotes/stream ----

async def test_stream_endpoint_without_token_returns_401(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    manager: SubscriptionManager,
    cp_gateway_mock,
) -> None:
    application = create_app(store=store, lifecycle=lifecycle, subscription_manager=manager)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.get("/v1/quotes/stream", params={"conids": "265598"})
    assert response.status_code == 401


async def test_stream_endpoint_with_wrong_scope_returns_403(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    manager: SubscriptionManager,
    cp_gateway_mock,
) -> None:
    bad = generate_token_value()
    store.put(Token(value=bad, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))
    application = create_app(store=store, lifecycle=lifecycle, subscription_manager=manager)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.get(
                "/v1/quotes/stream",
                params={"conids": "265598"},
                headers={"Authorization": f"Bearer {bad}"},
            )
    assert response.status_code == 403


async def test_stream_endpoint_above_limit_returns_429(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_client: CPGatewayClient,
    cp_gateway_mock,
) -> None:
    small_manager = SubscriptionManager(
        cp_client, poll_interval_s=10.0, cool_down_s=0.1, max_conids=1
    )
    try:
        # Belege 1/1 Slot vorab.
        it = await small_manager.subscribe([1001], [FIELD_ALIASES["last"]], "warmup")

        application = create_app(
            store=store, lifecycle=lifecycle, subscription_manager=small_manager
        )
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            async with application.router.lifespan_context(application):
                response = await ac.get(
                    "/v1/quotes/stream",
                    params={"conids": "9999"},
                    headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
                )
        assert response.status_code == 429
        assert response.headers["retry-after"] == "30"
        await it.aclose()
    finally:
        await small_manager.shutdown()


