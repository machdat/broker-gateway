"""Tests fuer ``broker_gateway.streams.orders.OrdersBroadcaster`` und
den ``/v1/orders/stream``-SSE-Endpoint (AP-11 K6).
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_ORDERS_READ,
    SCOPE_ORDERS_WRITE,
    SCOPE_QUOTES_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.cp.topics.sor import SorFrame
from broker_gateway.main import create_app
from broker_gateway.streams.orders import (
    OrdersBroadcaster,
    OrderStreamEvent,
    get_orders_broadcaster,
)


@pytest.fixture(autouse=True)
async def _reap_cool_down_tasks():
    """Raeumt nach jedem Test verwaiste Cool-Down-Tasks ab.

    Der Cool-Down (Karte f43a1514) startet beim Wegfall des letzten Consumers
    einen Timer-Task. Aeltere Logik-Tests, die ihren Broadcaster nicht explizit
    ``shutdown()``en und ihre Iteratoren dem GC ueberlassen, wuerden diesen
    Task sonst als „Task was destroyed but it is pending" in den Loop-Teardown
    schleppen. Produktiv erledigt das der Lifespan-Shutdown
    (``OrdersBroadcaster.shutdown`` in main.py). Der Reaper zwingt die
    Test-Generatoren zur Finalisierung und cancelt die entstandenen
    Cool-Down-Tasks noch im lebenden Loop.
    """
    import gc  # noqa: PLC0415

    yield
    gc.collect()  # Test-Generatoren finalisieren -> geplante aclose/detach
    # Mehrere Ticks: die geplanten asyncgen-aclose-Tasks laufen und erzeugen
    # ihrerseits die Cool-Down-Tasks, die wir danach canceln.
    for _ in range(5):
        await asyncio.sleep(0)
    pending = [
        t
        for t in asyncio.all_tasks()
        if t is not asyncio.current_task()
        and t.get_name().startswith("orders-cool-down-")
    ]
    for t in pending:
        t.cancel()
    for t in pending:
        with contextlib.suppress(BaseException):
            await t


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
    assert event.payload["order_id"] == "2"
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


# ---------------------------------------------------------------------------
# OrdersBroadcaster - Dedup (Karte 736c49a5)
# ---------------------------------------------------------------------------


async def _publish_then_replay(bc: OrdersBroadcaster, frames: list[SorFrame]) -> list:
    """Publisht ``frames`` und liefert, was ein frisch verbundener Consumer
    aus dem Ringpuffer-Replay sieht.

    Der Replay-Puffer enthaelt exakt die tatsaechlich fan-outeten (also
    deduplizierten) Events. Ein separater Replay-Consumer hat eine leere
    Live-Queue und sieht darum jedes Event genau einmal - anders als der
    publish-vor-iterieren-Pfad, in dem ein Event sowohl im Replay als auch in
    der Queue desselben Consumers auftaucht.
    """
    # Ein Live-Consumer muss existieren, sonst ist publish ein No-op.
    await bc.subscribe("U1", "keepalive", bootstrap=None)
    for frame in frames:
        bc.publish("U1", frame)
    replay = await bc.subscribe("U1", "replay", bootstrap=None)
    return await _collect(replay, max_events=len(frames) + 1)


async def test_broadcaster_dedups_identical_consecutive_frames() -> None:
    # Die drei ib_async-Callbacks publishen denselben Trade-Zustand als
    # byte-identische Frames - der Konsument darf sie nur einmal sehen.
    bc = OrdersBroadcaster()
    ts = "2026-07-15T21:07:13.319738+00:00"
    frames = [
        SorFrame(order_id=7, account="U1", symbol="AAPL", status="accepted",
                 last_event_at=ts)
        for _ in range(3)
    ]
    events = await _publish_then_replay(bc, frames)
    assert len(events) == 1
    assert events[0].payload["order_id"] == "7"


async def test_broadcaster_dedups_when_only_last_event_at_differs() -> None:
    # last_event_at allein taugt nicht als Unterscheidung: aendert sich NUR
    # der Zeitstempel, ist der Frame fuer den Konsumenten ein No-op.
    bc = OrdersBroadcaster()
    frames = [
        SorFrame(order_id=7, account="U1", status="accepted",
                 last_event_at="2026-07-15T21:07:13.100000+00:00"),
        SorFrame(order_id=7, account="U1", status="accepted",
                 last_event_at="2026-07-15T21:07:13.900000+00:00"),
    ]
    events = await _publish_then_replay(bc, frames)
    assert len(events) == 1


async def test_broadcaster_forwards_changed_status() -> None:
    # Ein echter Statuswechsel ist kein Duplikat und muss durchkommen.
    bc = OrdersBroadcaster()
    frames = [
        SorFrame(order_id=7, account="U1", status="accepted"),
        SorFrame(order_id=7, account="U1", status="cancelled"),
    ]
    events = await _publish_then_replay(bc, frames)
    assert [e.payload["status"] for e in events] == ["accepted", "cancelled"]


async def test_broadcaster_does_not_dedup_across_order_ids() -> None:
    # Dedup ist pro order_id - zwei verschiedene Orders im selben Zustand
    # sind keine Duplikate.
    bc = OrdersBroadcaster()
    frames = [
        SorFrame(order_id=1, account="U1", status="accepted"),
        SorFrame(order_id=2, account="U1", status="accepted"),
    ]
    events = await _publish_then_replay(bc, frames)
    assert sorted(e.payload["order_id"] for e in events) == ["1", "2"]


async def test_broadcaster_dedup_cache_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Der Dedup-Merker darf bei einer Dauerverbindung (refcount bleibt >=1)
    # nicht unbegrenzt wachsen - die aeltesten, laengst ruhenden order_ids
    # altern als LRU heraus (Review-Befund).
    import broker_gateway.streams.orders as orders_mod

    monkeypatch.setattr(orders_mod, "_DEDUP_CACHE_SIZE", 3)
    bc = OrdersBroadcaster()
    await bc.subscribe("U1", "keepalive", bootstrap=None)
    for oid in range(6):
        bc.publish("U1", SorFrame(order_id=oid, account="U1", status="accepted"))
    sub = bc._subs["U1"]
    # Gedeckelt auf 3, und es bleiben die drei juengsten order_ids. Die Keys
    # sind Strings, seit der Payload order_id als String traegt (c1c159d1).
    assert len(sub._last_order_key) == 3
    assert set(sub._last_order_key.keys()) == {"3", "4", "5"}


async def test_broadcaster_active_order_survives_lru_pressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Eine weiter aktive Order darf trotz LRU-Druck NICHT evakuiert werden:
    # jeder neue Frame frischt sie ans Ende der LRU auf.
    import broker_gateway.streams.orders as orders_mod

    monkeypatch.setattr(orders_mod, "_DEDUP_CACHE_SIZE", 3)
    bc = OrdersBroadcaster()
    await bc.subscribe("U1", "keepalive", bootstrap=None)
    # order_id=99 bleibt aktiv, dazwischen laufen viele andere Orders.
    for oid in range(6):
        bc.publish("U1", SorFrame(order_id=99, account="U1", status="accepted",
                                  last_event_at=f"t{oid}"))
        bc.publish("U1", SorFrame(order_id=oid, account="U1", status="accepted"))
    sub = bc._subs["U1"]
    assert "99" in sub._last_order_key
    assert len(sub._last_order_key) == 3


async def test_frame_payload_includes_raw_status() -> None:
    # Karte a04adca8: der rohe IBKR-Status laeuft additiv im Frame-Payload mit.
    from broker_gateway.streams.orders import _frame_to_payload

    payload = _frame_to_payload(
        SorFrame(order_id=1, account="U1", status="pending", raw_status="PendingCancel")
    )
    assert payload["status"] == "pending"
    assert payload["raw_status"] == "PendingCancel"


async def test_broadcaster_does_not_dedup_distinct_raw_status() -> None:
    # raw_status im Payload macht einen Uebergang PendingCancel -> Inactive
    # (beide status='pending') wieder als Nicht-Duplikat sichtbar, statt ihn
    # zu verschlucken (Karte a04adca8 x 736c49a5-Dedup).
    bc = OrdersBroadcaster()
    frames = [
        SorFrame(order_id=7, account="U1", status="pending", raw_status="PendingCancel"),
        SorFrame(order_id=7, account="U1", status="pending", raw_status="Inactive"),
    ]
    events = await _publish_then_replay(bc, frames)
    assert [e.payload["raw_status"] for e in events] == ["PendingCancel", "Inactive"]


# ---------------------------------------------------------------------------
# order_id-Typ im Wire-Vertrag (Karte c1c159d1)
# ---------------------------------------------------------------------------


async def test_frame_payload_order_id_ist_string() -> None:
    # Karte c1c159d1: order_id lief im Frame als JSON-Zahl, in der REST-Antwort
    # als String - derselbe Wert in zwei Typen. Der Stream zieht auf String nach.
    from broker_gateway.streams.orders import _frame_to_payload

    payload = _frame_to_payload(SorFrame(order_id=380195763, account="U1"))
    assert payload["order_id"] == "380195763"
    assert isinstance(payload["order_id"], str)


async def test_bootstrap_payload_order_id_ist_string() -> None:
    # Der Bootstrap-Frame laeuft durch dieselbe Serialisierung und darf nicht
    # den alten Zahl-Typ behalten - sonst wechselt der Typ mitten im Stream.
    bc = OrdersBroadcaster()
    iterator = await bc.subscribe(
        account="U1",
        consumer_id="c1",
        bootstrap=[SorFrame(order_id=1211, account="U1", status="accepted")],
    )
    event = await asyncio.wait_for(_first(iterator), timeout=1.0)
    assert event.event_type == "bootstrap"
    assert event.payload["orders"][0]["order_id"] == "1211"


async def test_stream_und_rest_tragen_order_id_im_selben_typ() -> None:
    """Der eigentliche Vertragstest: beide Flaechen, ein JSON-Typ.

    Nagelt fest, was die Karte entschieden hat - ein Konsument, der ueber
    ``GET /v1/orders`` und ``/v1/orders/stream`` korreliert, sieht denselben
    Typ. Der Wert selbst darf abweichen (POST liefert die orderId, das Listen
    die permId), das ist eine andere Baustelle.
    """
    from broker_gateway.order_models import Order, OrderSide, OrderStatus, OrderType, TimeInForce
    from broker_gateway.streams.orders import _frame_to_payload

    rest_order = Order(
        order_id="380195763",
        account_id="U1",
        conid=265598,
        side=OrderSide.SELL,
        quantity="1",
        order_type=OrderType.STP,
        tif=TimeInForce.GTC,
        status=OrderStatus.SUBMITTED,
    )
    frame_payload = _frame_to_payload(SorFrame(order_id=380195763, account="U1"))
    assert type(frame_payload["order_id"]) is type(rest_order.order_id)
    assert frame_payload["order_id"] == rest_order.order_id


# ---------------------------------------------------------------------------
# OrdersBroadcaster - Cool-Down / Last-Event-ID-Durability (Karte f43a1514)
# ---------------------------------------------------------------------------
#
# Hinweis: ``aclose()`` auf einem noch nicht gestarteten Async-Generator
# ueberspringt dessen ``finally`` - also auch das ``detach``. Jeder Test treibt
# deshalb erst einen ``__anext__`` (via ``_first``/``_collect``), bevor er den
# Iterator schliesst, damit der Cool-Down-Pfad ueberhaupt ausgeloest wird.


async def test_reconnect_within_cool_down_replays_gap_event() -> None:
    """Reconnect binnen Cool-Down findet Ringpuffer + Event-Zaehler intakt und
    bekommt ein waehrend der Luecke publishtes Event via Last-Event-ID
    nachgeliefert - genau der Fall der Karte: ein Fill faellt in die
    Reconnect-Pause und darf dem Konsumenten nicht verloren gehen."""
    bc = OrdersBroadcaster(cool_down_s=30.0)  # lang: Teardown feuert nie im Test
    try:
        it1 = await bc.subscribe("U1", "c1", bootstrap=None)
        bc.publish("U1", SorFrame(order_id=1, account="U1", status="accepted"))
        e0 = await asyncio.wait_for(_first(it1), timeout=1.0)  # id=0
        assert e0.event_id == 0
        sub_before = bc._subs["U1"]

        # c1 verschwindet -> Cool-Down startet, Subscription bleibt warm.
        await it1.aclose()
        assert "U1" in bc.active_accounts, "Cool-Down haelt die Subscription nicht warm"

        # LUECKE: Order fillt vollstaendig, waehrend niemand verbunden ist.
        bc.publish("U1", SorFrame(order_id=1, account="U1", status="filled"))  # id=1

        # Reconnect binnen Cool-Down mit Last-Event-ID=0.
        it2 = await bc.subscribe("U1", "c2", bootstrap=None, last_event_id=0)
        assert bc._subs["U1"] is sub_before, "Reconnect traf eine frische Subscription"
        assert sub_before._cool_down_task is None, "Cool-Down-Task nicht gecancelt (Leak)"

        events = await _collect(it2, max_events=1)
        assert [e.event_id for e in events] == [1]
        assert events[0].payload["status"] == "filled"
        await it2.aclose()
    finally:
        await bc.shutdown()


async def test_teardown_after_cool_down_drops_subscription() -> None:
    """Laeuft der Cool-Down ohne Reconnect ab, wird die Subscription (mit
    Ringpuffer + Zaehler) verworfen - der zugesagte Replay traegt dann nicht
    mehr, ein Reconnect faengt bei Zaehler 0 an (Stufe C: Konsument muss via
    REST reconcilen)."""
    bc = OrdersBroadcaster(cool_down_s=0.0)  # Teardown nach einem Loop-Tick
    it = await bc.subscribe(
        "U1", "c1", bootstrap=[SorFrame(order_id=1, account="U1", status="accepted")]
    )
    sub = bc._subs["U1"]
    await asyncio.wait_for(_first(it), timeout=1.0)  # Generator starten (bootstrap id=0)
    await it.aclose()  # detach -> Cool-Down-Task (sleep 0)
    task = sub._cool_down_task
    assert task is not None
    await task  # Cool-Down durchlaufen lassen -> _teardown poppt die Subscription
    assert "U1" not in bc.active_accounts

    # Reconnect nach Teardown -> frische Subscription, Zaehler zurueck auf 0.
    it2 = await bc.subscribe("U1", "c2", bootstrap=None, last_event_id=5)
    assert bc._subs["U1"] is not sub
    assert bc._subs["U1"]._next_event_id == 0
    await it2.aclose()
    await bc.shutdown()


async def test_teardown_is_noop_when_consumer_reattached() -> None:
    """Reconnect zwischen Cool-Down-Ablauf und _teardown: der refcount-Recheck
    unter Lock verhindert, dass eine wieder benutzte Subscription abgeraeumt
    wird (Race-Absicherung, deckungsgleich mit streams/manager.py)."""
    bc = OrdersBroadcaster(cool_down_s=30.0)
    try:
        it1 = await bc.subscribe(
            "U1", "c1",
            bootstrap=[SorFrame(order_id=1, account="U1", status="accepted")],
        )
        sub = bc._subs["U1"]
        await asyncio.wait_for(_first(it1), timeout=1.0)
        await it1.aclose()  # refcount 0, Cool-Down laeuft
        assert sub.refcount == 0

        # Neuer Consumer haengt sich an, BEVOR wir _teardown von Hand ausloesen.
        it2 = await bc.subscribe("U1", "c2", bootstrap=None)
        assert sub.refcount == 1
        assert sub._cool_down_task is None, "attach hat den Cool-Down nicht gecancelt"

        # _teardown direkt gerufen (simuliert einen bereits-abgelaufenen
        # Cool-Down, der die Lock-Runde gewinnt): darf NICHT abraeumen.
        await bc._teardown(sub)
        assert bc._subs.get("U1") is sub

        await it2.aclose()
    finally:
        await bc.shutdown()


async def test_shutdown_cancels_pending_cool_down() -> None:
    """shutdown() cancelt offene Cool-Down-Tasks - kein Task ueberdauert den
    Prozess-Exit (Lifespan-Cleanup, main.py)."""
    bc = OrdersBroadcaster(cool_down_s=30.0)
    it = await bc.subscribe(
        "U1", "c1", bootstrap=[SorFrame(order_id=1, account="U1", status="accepted")]
    )
    sub = bc._subs["U1"]
    await asyncio.wait_for(_first(it), timeout=1.0)
    await it.aclose()
    task = sub._cool_down_task
    assert task is not None and not task.done()

    await bc.shutdown()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    # Vertrag: kein Cool-Down-Task ueberdauert den Shutdown. task.done() ist
    # robust gegen die Cancellation-Propagation - ob die CancelledError bis zum
    # Task-Frame durchschlaegt (Task noch nicht gestartet -> cancelled()) oder
    # vom eigenen except geschluckt wird (Task schon im sleep -> Ergebnis None),
    # haengt an einer Scheduling-Feinheit und ist nicht der eigentliche Punkt.
    assert task.done()
    assert bc.active_accounts == set()


async def _first(iterator) -> OrderStreamEvent:
    async for event in iterator:
        return event
    raise AssertionError("Iterator hat kein Event geliefert")


async def _collect(iterator, *, max_events: int, timeout: float = 0.5) -> list:
    """Sammelt bis zu ``max_events`` Events; ein Timeout bedeutet 'nichts mehr
    da' (die Dedup hat den erwarteten Frame verworfen)."""
    events: list = []
    try:
        while len(events) < max_events:
            events.append(
                await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
            )
    except (asyncio.TimeoutError, StopAsyncIteration):
        pass
    return events


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
    """Ohne Scope 403.

    Achtung: Dieser Test allein belegt NICHT, dass ``/orders/stream``
    routbar ist. ``GET /orders/{order_id}`` verlangt ebenfalls einen
    Scope und antwortet auf denselben Token identisch mit 403 - der
    Test war deshalb auch dann grün, als die Route von der
    Platzhalter-Route verschluckt wurde (Karte ``cefcb57a``). Die
    Routbarkeit nagelt ``TestOrdersStreamRouting`` fest.
    """
    no_scope = "orders-no-scope-aaaaaaaaaaaaaaaaaa"
    store.put(
        Token(value=no_scope, caller_id="other", scopes=[])
    )
    response = client.get(
        "/v1/orders/stream?account=U25235077",
        headers={"Authorization": f"Bearer {no_scope}"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# /v1/orders/stream - Scope-Semantik (Karte 601c6e09)
# ---------------------------------------------------------------------------


class TestOrdersStreamScopes:
    """``orders:write`` schließt das Leserecht am Stream mit ein.

    Der Stream verlangte strikt ``orders:read``, während alle lesenden
    REST-Endpunkte ``read`` ODER ``write`` akzeptieren. Damit sperrte
    ausgerechnet der Event-Pfad einen Konsumenten aus, der dieselben
    Orders über ``GET /v1/orders`` längst lesen darf - trading_robot
    hat genau diesen Zuschnitt (``orders:write``, kein ``orders:read``).
    """

    def test_write_only_token_reaches_the_real_stream(
        self, client: TestClient, store: InMemoryTokenStore
    ) -> None:
        """Ein Token mit NUR orders:write bekommt den echten SSE-Stream.

        Prüft bewusst den Content-Type und nicht bloß „kein 403": Die
        Lehre aus Karte ``cefcb57a`` ist, dass eine Assertion auf einen
        Status-Code allein nicht verrät, welcher Handler geantwortet
        hat. Erst ``text/event-stream`` beweist, dass der Token im
        Stream angekommen ist.
        """
        from broker_gateway.api.v1.orders_stream import (  # noqa: PLC0415
            get_orders_bootstrap_loader,
        )

        write_only = "orders-write-only-aaaaaaaaaaaaaaaaaa"
        store.put(
            Token(
                value=write_only,
                caller_id="trading-robot-like",
                scopes=[SCOPE_ORDERS_WRITE],
            )
        )

        with _overridden(
            client,
            {
                get_orders_bootstrap_loader: lambda: _EmptyBootstrapLoader(),
                get_orders_broadcaster: lambda: _FiniteBroadcaster(),
            },
        ):
            response = client.get(
                "/v1/orders/stream?account=U25235077",
                headers={"Authorization": f"Bearer {write_only}"},
            )

        content_type = response.headers.get("content-type", "")
        assert response.status_code == 200, (
            f"write-only-Token abgewiesen: {response.status_code} "
            f"{response.text[:200]}"
        )
        assert content_type.startswith("text/event-stream"), (
            f"Erwartet text/event-stream, bekam {content_type!r}"
        )

    def test_read_only_token_still_reaches_the_stream(
        self, client: TestClient, store: InMemoryTokenStore
    ) -> None:
        """Gegenrichtung: orders:read funktioniert unverändert weiter."""
        from broker_gateway.api.v1.orders_stream import (  # noqa: PLC0415
            get_orders_bootstrap_loader,
        )

        read_only = "orders-read-only-aaaaaaaaaaaaaaaaaaa"
        store.put(
            Token(
                value=read_only,
                caller_id="reader",
                scopes=[SCOPE_ORDERS_READ],
            )
        )

        with _overridden(
            client,
            {
                get_orders_bootstrap_loader: lambda: _EmptyBootstrapLoader(),
                get_orders_broadcaster: lambda: _FiniteBroadcaster(),
            },
        ):
            response = client.get(
                "/v1/orders/stream?account=U25235077",
                headers={"Authorization": f"Bearer {read_only}"},
            )

        assert response.status_code == 200
        assert response.headers.get("content-type", "").startswith(
            "text/event-stream"
        )

    def test_unrelated_scope_is_still_rejected(
        self, client: TestClient, store: InMemoryTokenStore
    ) -> None:
        """Die Weitung ist auf orders:* begrenzt - kein Freifahrtschein.

        Ein Token mit einem fremden Lese-Scope darf den Order-Stream
        weiterhin nicht sehen.
        """
        wrong_scope = "orders-wrong-scope-aaaaaaaaaaaaaaaa"
        store.put(
            Token(
                value=wrong_scope,
                caller_id="quotes-only",
                scopes=[SCOPE_QUOTES_READ],
            )
        )
        response = client.get(
            "/v1/orders/stream?account=U25235077",
            headers={"Authorization": f"Bearer {wrong_scope}"},
        )

        assert response.status_code == 403


# ---------------------------------------------------------------------------
# /v1/orders/stream - Routbarkeit (Karte cefcb57a)
# ---------------------------------------------------------------------------


class _EmptyBootstrapLoader:
    """Bootstrap-Loader, der nichts lädt - hält den CP-Mock aus dem Spiel."""

    async def load(self, account: str) -> list[Any]:
        return []


class _FiniteBroadcaster:
    """Broadcaster mit ENDLICHEM Iterator, der seinen Aufruf protokolliert.

    Der echte Broadcaster liefert einen endlosen Iterator. Starlettes
    TestClient sammelt aber den kompletten Body, bevor er die Response
    herausgibt - gegen den echten Stream hängt jeder Test, auch mit
    ``client.stream`` (empirisch verifiziert). Deshalb hier eine
    endliche Quelle: Route, Handler und SSE-Formatierung laufen
    unverändert echt, nur die Events sind endlich.

    ``last_subscribe_kwargs`` macht sichtbar, was der Endpunkt an
    ``subscribe`` durchgereicht hat.
    """

    def __init__(self) -> None:
        self.last_subscribe_kwargs: dict[str, Any] | None = None

    async def subscribe(
        self,
        account: str,
        consumer_id: str,
        *,
        bootstrap: Any = None,
        last_event_id: int | None = None,
    ):
        self.last_subscribe_kwargs = {
            "account": account,
            "bootstrap": bootstrap,
            "last_event_id": last_event_id,
        }

        async def _gen():
            yield OrderStreamEvent(
                event_id=1,
                account=account,
                event_type="bootstrap",
                payload={"orders": []},
            )

        return _gen()


@contextlib.contextmanager
def _overridden(client: TestClient, overrides: dict[Any, Any]):
    """Setzt ``dependency_overrides`` und stellt den Vorzustand wieder her.

    Wichtig ist das Wiederherstellen statt bloßem Löschen: ``create_app``
    installiert für einige dieser Dependencies selbst Overrides auf
    ``app.state``. Ein blindes ``pop`` würde die wegwerfen. Heute
    folgenlos, weil die ``client``-Fixture pro Test eine frische App
    baut - aber genau die Sorte Fallstrick, die beim Hochziehen der
    Fixture auf Modul-Scope still zuschlägt.
    """
    registry = client.app.dependency_overrides
    sentinel = object()
    previous = {key: registry.get(key, sentinel) for key in overrides}
    registry.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is sentinel:
                registry.pop(key, None)
            else:
                registry[key] = value


class TestOrdersStreamRouting:
    """``/v1/orders/stream`` darf nicht von ``/orders/{order_id}`` verdeckt werden.

    Der Bug: ``orders_router`` (mit ``GET /{order_id}``) war vor
    ``orders_stream_router`` registriert. Starlette nimmt den ersten
    Treffer in Registrierungsreihenfolge, also landete jeder Aufruf von
    ``/orders/stream`` in ``get_order`` mit ``order_id="stream"`` und
    bekam 404 ``order_id unbekannt``.

    Diese Tests laufen bewusst gegen die zusammengesetzte App und rufen
    die Endpunkte wirklich auf. Zwei Sackgassen, die hier schon
    besichtigt wurden:

    - Ein Test gegen die Handler-Funktion allein prüft am Bug vorbei -
      der Handler war nie defekt, nur unerreichbar.
    - Ein Test, der Starlettes Routen-Tabelle introspiziert
      (``app.routes``, ``route.endpoint``), ist an Framework-Interna
      gekoppelt und zerbricht an Versionssprüngen: lokal (FastAPI 0.135)
      liegen die Routen flach, ab 0.139 - CI und Produktion - steckt ein
      ``_IncludedRouter`` davor, der sie nicht herausgibt. Deshalb hier
      ausschließlich echte Requests.
    """

    def test_stream_with_valid_token_yields_event_stream_not_json_404(
        self, client: TestClient
    ) -> None:
        """Der Aufruf liefert text/event-stream statt des JSON-404.

        Deckt die ersten beiden Verification-Punkte der Karte in einem
        Zug ab: richtiger Content-Type, und nie wieder das
        ``order_id unbekannt``-404 aus ``get_order``.

        Bootstrap-Loader und Broadcaster werden per
        ``dependency_overrides`` ersetzt - beides sind die dafür
        vorgesehenen Extension-Points (die echten Funktionen werfen ohne
        Override). Warum der Broadcaster endlich sein muss, steht bei
        ``_FiniteBroadcaster``. Route, Handler und SSE-Formatierung
        laufen unverändert echt.
        """
        from broker_gateway.api.v1.orders_stream import (  # noqa: PLC0415
            get_orders_bootstrap_loader,
        )

        with _overridden(
            client,
            {
                get_orders_bootstrap_loader: lambda: _EmptyBootstrapLoader(),
                get_orders_broadcaster: lambda: _FiniteBroadcaster(),
            },
        ):
            response = client.get(
                "/v1/orders/stream?account=U25235077",
                headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
            )

        content_type = response.headers.get("content-type", "")
        assert "order_id unbekannt" not in response.text, (
            "Der Aufruf landete in GET /orders/{order_id} - "
            "die Stream-Route ist verdeckt."
        )
        assert response.status_code == 200, (
            f"Erwartet 200, bekam {response.status_code} "
            f"(content-type {content_type!r})"
        )
        assert content_type.startswith("text/event-stream"), (
            f"Erwartet text/event-stream, bekam {content_type!r}"
        )
        assert "event: bootstrap" in response.text

    def test_last_event_id_header_reaches_the_broadcaster(
        self, client: TestClient
    ) -> None:
        """Der ``Last-Event-ID``-Header landet als ``last_event_id`` im subscribe.

        Der Reconnect-Vertrag hängt an dieser einen Durchreichung. Die
        Broadcaster-Logik dahinter ist separat unit-getestet, die
        Verdrahtung Header -> Handler -> ``subscribe`` bisher nicht.
        Bricht der Header-Alias, fällt jeder Reconnect still auf „von
        vorn" zurück: der Konsument bekommt Bootstrap statt Delta, ohne
        jede Fehlermeldung. Diese Route sieht gerade zum ersten Mal
        echte Konsumenten - der Test schließt die Lücke, bevor sie
        jemand findet.
        """
        from broker_gateway.api.v1.orders_stream import (  # noqa: PLC0415
            get_orders_bootstrap_loader,
        )

        broadcaster = _FiniteBroadcaster()
        with _overridden(
            client,
            {
                get_orders_bootstrap_loader: lambda: _EmptyBootstrapLoader(),
                get_orders_broadcaster: lambda: broadcaster,
            },
        ):
            response = client.get(
                "/v1/orders/stream?account=U25235077",
                headers={
                    "Authorization": f"Bearer {_ADMIN_VALUE}",
                    "Last-Event-ID": "42",
                },
            )

        assert response.status_code == 200
        assert broadcaster.last_subscribe_kwargs is not None, (
            "subscribe wurde nie gerufen"
        )
        assert broadcaster.last_subscribe_kwargs["last_event_id"] == 42, (
            "Der Last-Event-ID-Header kam nicht am Broadcaster an: "
            f"{broadcaster.last_subscribe_kwargs!r}"
        )
        assert broadcaster.last_subscribe_kwargs["account"] == "U25235077"

    def test_stream_without_token_is_rejected_by_auth_not_by_404(
        self, client: TestClient
    ) -> None:
        """Ohne Token greift Auth (401), nicht das 404 der Platzhalter-Route.

        Vierter Verification-Punkt der Karte. Wie bei
        ``test_orders_stream_requires_orders_read_scope`` gilt: dieser
        Test allein belegt die Routbarkeit NICHT - ohne Token antworten
        beide Handler identisch 401, weil die Auth-Dependency wirft,
        bevor irgendetwas Routen-Spezifisches passiert. Der Wert liegt
        woanders: er nagelt fest, dass die Stream-Route nicht
        versehentlich am Auth vorbeigeführt wird. Die Routbarkeit
        beweisen die beiden Tests darüber.
        """
        response = client.get("/v1/orders/stream?account=U25235077")

        assert response.status_code in (401, 403), (
            f"Erwartet 401/403, bekam {response.status_code}: "
            f"{response.text[:200]}"
        )

    def test_get_order_with_real_id_still_resolves_to_get_order(
        self, client: TestClient
    ) -> None:
        """Regression: eine echte Order-ID geht weiter an ``get_order``.

        Der Fix verschiebt nur die Reihenfolge - die Platzhalter-Route
        selbst bleibt unverändert erreichbar. Die Gegenrichtung des
        Bugs: die Stream-Route darf keine echten Order-IDs schlucken.
        Ein SSE-Response hier wäre der Beweis, dass sie es tut.

        Dass ``get_order`` sich auch inhaltlich unverändert verhält,
        decken die bestehenden Tests in ``tests/test_orders.py`` mit
        echten Requests ab.
        """
        response = client.get(
            "/v1/orders/1234567",
            headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
        )

        content_type = response.headers.get("content-type", "")
        assert not content_type.startswith("text/event-stream"), (
            "GET /v1/orders/<id> landete im SSE-Stream - die Stream-Route "
            "schluckt echte Order-IDs."
        )
        assert content_type.startswith("application/json"), (
            f"Erwartet eine JSON-Antwort aus get_order, bekam {content_type!r}"
        )


# ---------------------------------------------------------------------------
# /v1/orders/stream - Heartbeat-Verdrahtung (Karte 9b1d76ba, Nachbesserung)
# ---------------------------------------------------------------------------


def test_orders_stream_verdrahtet_heartbeat_wrapper(
    client: TestClient,
    store: InMemoryTokenStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Beweist die Verdrahtung: der Endpunkt ruft ``sse_with_heartbeat`` (nicht
    das alte ``_to_sse``). Ohne diesen Test bliebe ein stiller Revert auf
    ``_to_sse`` unbemerkt - die uebrigen Endpunkt-Tests waeren byte-identisch
    gruen (die Reconnect-Sturm-Regression kaeme ungefangen zurueck)."""
    from broker_gateway.api.v1 import orders_stream as mod
    from broker_gateway.api.v1.orders_stream import (  # noqa: PLC0415
        get_orders_bootstrap_loader,
    )

    calls: list[dict[str, Any]] = []

    def _spy(source: Any, render: Any, **kwargs: Any) -> Any:
        calls.append({"render": render, "kwargs": kwargs})

        async def _finite() -> Any:
            await source.aclose()  # echte Quelle sauber schliessen, nicht konsumieren
            return
            yield b""  # macht die Fn zum async generator (unreachable)

        return _finite()

    monkeypatch.setattr(mod, "sse_with_heartbeat", _spy)
    with _overridden(
        client,
        {
            get_orders_bootstrap_loader: lambda: _EmptyBootstrapLoader(),
            get_orders_broadcaster: lambda: _FiniteBroadcaster(),
        },
    ):
        response = client.get(
            "/v1/orders/stream?account=U25235077",
            headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
        )
    assert response.status_code == 200
    assert len(calls) == 1, (
        "sse_with_heartbeat wurde nicht aufgerufen - der Heartbeat-Wrapper "
        "ist nicht mehr verdrahtet"
    )
    assert callable(calls[0]["render"])


def test_orders_stream_sendet_keepalive_bei_stille(
    client: TestClient,
    store: InMemoryTokenStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-End: ein stiller Order-Stream sendet nachweislich einen
    Keepalive-Comment - der eigentliche Vertrag der Karte 9b1d76ba, am echten
    Endpunkt belegt (nicht nur an der isolierten Wrapper-Funktion). Das
    Intervall wird per monkeypatch auf 0.02s gesetzt, damit der Heartbeat im
    Test schnell feuert."""
    import functools  # noqa: PLC0415

    from broker_gateway.api.v1 import orders_stream as mod
    from broker_gateway.api.v1.orders_stream import (  # noqa: PLC0415
        get_orders_bootstrap_loader,
    )
    from broker_gateway.streams.heartbeat import (  # noqa: PLC0415
        sse_with_heartbeat as _real,
    )

    class _SilentThenEvent:
        async def subscribe(
            self,
            account: str,
            consumer_id: str,
            *,
            bootstrap: Any = None,
            last_event_id: Any = None,
        ) -> Any:
            async def _gen() -> Any:
                await asyncio.sleep(0.05)  # Stille > interval 0.02 -> Heartbeat(s)
                yield OrderStreamEvent(
                    event_id=0,
                    account=account,
                    event_type="order",
                    payload={"order_id": 7},
                )

            return _gen()

    monkeypatch.setattr(
        mod, "sse_with_heartbeat", functools.partial(_real, interval_s=0.02)
    )
    with _overridden(
        client,
        {
            get_orders_bootstrap_loader: lambda: _EmptyBootstrapLoader(),
            get_orders_broadcaster: lambda: _SilentThenEvent(),
        },
    ):
        response = client.get(
            "/v1/orders/stream?account=U25235077",
            headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
        )
    assert response.status_code == 200
    body = response.text
    assert ": keepalive" in body, f"kein Heartbeat im stillen Stream: {body!r}"
    assert '"order_id":7' in body  # das Event kam nach der Stille auch durch
