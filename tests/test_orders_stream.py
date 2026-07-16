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
    assert events[0].payload["order_id"] == 7


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
    assert sorted(e.payload["order_id"] for e in events) == [1, 2]


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
    # Gedeckelt auf 3, und es bleiben die drei juengsten order_ids.
    assert len(sub._last_order_key) == 3
    assert set(sub._last_order_key.keys()) == {3, 4, 5}


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
    assert 99 in sub._last_order_key
    assert len(sub._last_order_key) == 3


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
