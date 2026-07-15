"""Tests fuer ``broker_gateway.streams.orders.OrdersBroadcaster`` und
den ``/v1/orders/stream``-SSE-Endpoint (AP-11 K6).
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Match

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
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
# /v1/orders/stream - Routbarkeit (Karte cefcb57a)
# ---------------------------------------------------------------------------


class TestOrdersStreamRouting:
    """``/v1/orders/stream`` darf nicht von ``/orders/{order_id}`` verdeckt werden.

    Der Bug: ``orders_router`` (mit ``GET /{order_id}``) war vor
    ``orders_stream_router`` registriert. Starlette nimmt den ersten
    Treffer in Registrierungsreihenfolge, also landete jeder Aufruf von
    ``/orders/stream`` in ``get_order`` mit ``order_id="stream"`` und
    bekam 404 ``order_id unbekannt``.

    Diese Tests laufen bewusst gegen die zusammengesetzte App. Ein Test
    gegen die Handler-Funktion allein würde am Bug vorbeiprüfen, weil
    der Handler selbst nie defekt war - nur unerreichbar.
    """

    def test_stream_path_resolves_to_stream_handler(
        self, client: TestClient
    ) -> None:
        """Der erste Routen-Treffer für GET /v1/orders/stream ist der Stream-Handler.

        Prüft die Ursache direkt an Starlettes Matching-Mechanik, ohne
        den Endpunkt auszuführen: welche Route greift zuerst?
        """
        from broker_gateway.api.v1.orders_stream import (  # noqa: PLC0415
            orders_stream,
        )

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/v1/orders/stream",
            "path_params": {},
            "root_path": "",
            "headers": [],
        }
        matched = [
            route
            for route in client.app.routes
            if route.matches(scope)[0] is Match.FULL
        ]
        assert matched, "Keine Route matcht GET /v1/orders/stream"
        assert matched[0].endpoint is orders_stream, (
            "Erster Treffer ist "
            f"{getattr(matched[0], 'endpoint', matched[0])!r} statt "
            "orders_stream - die Platzhalter-Route verdeckt den Stream."
        )

    def test_stream_with_valid_token_yields_event_stream_not_json_404(
        self, client: TestClient
    ) -> None:
        """Der Aufruf liefert text/event-stream statt des JSON-404.

        Deckt die ersten beiden Verification-Punkte der Karte in einem
        Zug ab: richtiger Content-Type, und nie wieder das
        ``order_id unbekannt``-404 aus ``get_order``.

        Zwei Dependencies werden per ``dependency_overrides`` ersetzt -
        beides sind die dafür vorgesehenen Extension-Points, die echten
        Funktionen werfen ohne Override:

        - Der Bootstrap-Loader, damit der CP-Mock aus dem Spiel bleibt.
        - Der Broadcaster durch einen mit ENDLICHEM Iterator. Das ist
          hier keine Bequemlichkeit, sondern nötig: Starlettes
          TestClient sammelt den kompletten Body, bevor er die Response
          herausgibt. Gegen den echten, endlos laufenden SSE-Generator
          hängt der Test - auch mit ``client.stream``. Verifiziert: mit
          dem echten Broadcaster lief er in den Timeout.

        Route, Handler und SSE-Formatierung laufen dabei unverändert
        echt - nur die Event-Quelle ist endlich.
        """
        from broker_gateway.api.v1.orders_stream import (  # noqa: PLC0415
            get_orders_bootstrap_loader,
        )

        class _EmptyBootstrapLoader:
            async def load(self, account: str) -> list[Any]:
                return []

        class _FiniteBroadcaster:
            async def subscribe(
                self,
                account: str,
                consumer_id: str,
                *,
                bootstrap: Any = None,
                last_event_id: int | None = None,
            ):
                async def _gen():
                    yield OrderStreamEvent(
                        event_id=1,
                        account=account,
                        event_type="bootstrap",
                        payload={"orders": []},
                    )

                return _gen()

        overrides = client.app.dependency_overrides
        previous_broadcaster = overrides.get(get_orders_broadcaster)
        overrides[get_orders_bootstrap_loader] = lambda: _EmptyBootstrapLoader()
        overrides[get_orders_broadcaster] = lambda: _FiniteBroadcaster()
        try:
            response = client.get(
                "/v1/orders/stream?account=U25235077",
                headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
            )
        finally:
            overrides.pop(get_orders_bootstrap_loader, None)
            if previous_broadcaster is None:
                overrides.pop(get_orders_broadcaster, None)
            else:
                overrides[get_orders_broadcaster] = previous_broadcaster

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

    def test_get_order_with_real_id_still_resolves_to_get_order(
        self, client: TestClient
    ) -> None:
        """Regression: eine echte Order-ID geht weiter an ``get_order``.

        Der Fix verschiebt nur die Reihenfolge - die Platzhalter-Route
        selbst bleibt unverändert erreichbar.
        """
        from broker_gateway.api.v1.orders import get_order  # noqa: PLC0415

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/v1/orders/1234567",
            "path_params": {},
            "root_path": "",
            "headers": [],
        }
        matched = [
            route
            for route in client.app.routes
            if route.matches(scope)[0] is Match.FULL
        ]
        assert matched, "Keine Route matcht GET /v1/orders/1234567"
        assert matched[0].endpoint is get_order
