"""Strukturelle Routing-Invariante der v1-API (Karte ``cefcb57a``).

Hintergrund: ``GET /v1/orders/stream`` war seit seiner Einführung
unerreichbar, weil ``orders_router`` mit seiner Platzhalter-Route
``GET /orders/{order_id}`` vor ``orders_stream_router`` registriert war.
Starlette nimmt den ersten Treffer in Registrierungsreihenfolge, also
landete jeder Stream-Aufruf in ``get_order`` mit ``order_id="stream"``
und bekam 404.

``tests/test_orders_stream.py::TestOrdersStreamRouting`` nagelt den
konkreten Fall fest. Dieses Modul sichert die Fehlerklasse ab: jede
statische HTTP-Route muss erreichbar sein, auch die, die es heute noch
nicht gibt. Punkt 4 des Soll-Zustands der Karte.

Reichweite bewusst begrenzt auf ``APIRoute``, also HTTP:

- WebSocket-Routen (``APIWebSocketRoute``, z.B. ``/v1/orders/ws``) sind
  nicht erfasst. Starlette matcht HTTP und WebSocket getrennt, sie
  können sich also nicht gegenseitig verdecken - genau deshalb war der
  WS-Zwilling vom Bug nie betroffen. Eine parametrisierte WS-Route, die
  eine statische WS-Route verdeckt, gibt es heute nicht; käme sie, wäre
  dieser Test blind dafür.
- Platzhalter-gegen-Platzhalter ist nicht abgedeckt. Die Invariante ist
  bewusst auf statische Routen formuliert, weil nur die eindeutig
  „erreichbar" sein können.
"""
from __future__ import annotations

from fastapi.routing import APIRoute
from starlette.routing import Match

from broker_gateway.main import create_app


def _http_routes() -> list[APIRoute]:
    """Alle HTTP-Routen der zusammengesetzten App in Registrierungsreihenfolge.

    ``create_app()`` ohne Argumente ist der Modul-Level-Pfad aus
    ``main.py`` und braucht kein Env - die Verbindungen entstehen erst
    im Lifespan, der hier bewusst nicht startet. Für die reine
    Routen-Tabelle genügt das.
    """
    app = create_app()
    return [route for route in app.routes if isinstance(route, APIRoute)]


def _first_match(routes: list[APIRoute], path: str, method: str) -> APIRoute | None:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "path_params": {},
        "root_path": "",
        "headers": [],
    }
    for route in routes:
        if route.matches(scope)[0] is Match.FULL:
            return route
    return None


def test_no_static_route_is_shadowed_by_a_placeholder_route() -> None:
    """Jede statische HTTP-Route ist unter jeder ihrer Methoden erreichbar.

    Eine statische Route (ohne ``{param}``) gilt als verdeckt, wenn für
    ihren eigenen Pfad und ihre eigene Methode eine ANDERE Route zuerst
    matcht - dann ist sie toter Code, egal was die openapi.json behauptet.
    Die openapi.json ist hier übrigens kein Zeuge: FastAPI generiert sie
    aus den registrierten Routen, nicht aus den erreichbaren. Genau
    deshalb ist der Bug durch jeden Doku-Drift-Review gerutscht.

    ``Match.FULL`` prüft Pfad UND Methode. Deshalb ist
    ``POST /orders/whatif`` neben ``GET /orders/{order_id}`` unauffällig
    und wird hier korrekt nicht gemeldet.
    """
    routes = _http_routes()
    static_routes = [route for route in routes if "{" not in route.path]

    shadowed: list[str] = []
    for route in static_routes:
        for method in sorted(route.methods or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            winner = _first_match(routes, route.path, method)
            if winner is not None and winner is not route:
                shadowed.append(
                    f"{method} {route.path} -> verdeckt von "
                    f"{sorted(winner.methods or set())} {winner.path} "
                    f"(Endpunkt {winner.endpoint.__name__})"
                )

    assert not shadowed, (
        "Statische Routen werden von früher registrierten "
        "Platzhalter-Routen verdeckt und sind damit unerreichbar. "
        "Router-Reihenfolge in api/v1/__init__.py prüfen:\n  "
        + "\n  ".join(shadowed)
    )


def test_orders_stream_is_registered_before_the_order_id_placeholder() -> None:
    """Die konkrete Reihenfolge, die der Bug verletzt hat.

    Redundant zum Klassen-Test oben, aber diagnostisch: schlägt dieser
    hier fehl, ist sofort klar, welche zwei Router betroffen sind.
    """
    routes = _http_routes()
    paths = [route.path for route in routes]

    assert "/v1/orders/stream" in paths, "Stream-Route ist nicht registriert"
    assert "/v1/orders/{order_id}" in paths, "Platzhalter-Route fehlt"
    assert paths.index("/v1/orders/stream") < paths.index(
        "/v1/orders/{order_id}"
    ), (
        "orders_stream_router muss vor orders_router registriert werden, "
        "sonst verschluckt GET /orders/{order_id} den Pfad /orders/stream."
    )
