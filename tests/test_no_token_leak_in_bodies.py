"""AP-05 K5: Kein Token-Wert taucht im JSON-Response-Body irgendeines
v1-Endpunkts auf.

Erzeugt ueber ``POST /v1/auth/token`` einen frischen Token mit hoher
Entropie und faehrt damit alle Read-Endpunkte an. Pro Endpunkt wird
geprueft, dass der Token-Wert weder im Body-Text noch in den Response-
Headern auftaucht. ``POST /v1/auth/token`` ist explizit ausgenommen -
dort ist der Token-Echo das designierte Verhalten.

Hintergrund: cp/redaction.py filtert Token aus Logs/Recordings, aber es
gibt heute keinen Schutz dagegen, dass ein Programmierfehler (falscher
``model_dump``-Pfad, ein Endpunkt der den ``Authorization``-Header in
einem Body echo't, ein neuer Endpunkt mit Token-Echo) den Token in den
Response-Body bringt. Dieser Test ist die Sicherheits-Invariante.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import (
    ALL_SCOPES,
    SCOPE_ADMIN_ALL,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import InstrumentsService
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.cp.orders import OrdersService
from broker_gateway.cp.portfolio import PortfolioService
from broker_gateway.cp.quotes import QuotesService
from broker_gateway.cp.trades import TradesService
from broker_gateway.main import create_app


_BOOTSTRAP_VALUE = "leak-test-bootstrap-aaaaaaaaaaaaaaaaaaaaaa"
_ACCOUNT_ID = "U25235077"


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(
        Token(
            value=_BOOTSTRAP_VALUE,
            caller_id="bootstrap-admin",
            scopes=[SCOPE_ADMIN_ALL],
        )
    )
    return s


@pytest.fixture
async def cp_client(cp_gateway_mock) -> CPGatewayClient:
    client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    yield client
    await client.aclose()


@pytest.fixture
async def lifecycle(cp_client: CPGatewayClient) -> AuthLifecycle:
    lc = AuthLifecycle(
        cp_client,
        tickle_interval_s=10.0,
        reauth_max_retries=1,
        reauth_backoff_s=0.0,
    )
    yield lc
    await lc.stop()


@pytest.fixture
async def client(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_client: CPGatewayClient,
    cp_gateway_mock,
):
    application = create_app(
        store=store,
        lifecycle=lifecycle,
        instruments_service=InstrumentsService(cp_client),
        quotes_service=QuotesService(cp_client, prime_delay_s=0.0),
        portfolio_service=PortfolioService(cp_client),
        orders_service=OrdersService(cp_client),
        trades_service=TradesService(cp_client),
    )
    with TestClient(application) as test_client:
        yield test_client


# Endpunkte (Methode, Pfad, Query) die als "Read" mit dem frischen Token
# durchgespielt werden. Streams (text/event-stream) sind out-of-scope -
# siehe Karten-Notiz; eigene Karte falls SSE-Token-Echo geprueft werden
# soll.
_READ_ENDPOINTS: list[tuple[str, str, dict[str, str]]] = [
    ("GET", "/v1/health", {}),
    ("GET", "/v1/internal/health", {}),
    ("GET", "/v1/instruments/search", {"q": "AAPL"}),
    ("GET", "/v1/instruments/265598", {}),
    ("GET", "/v1/quotes/snapshot", {"conids": "265598"}),
    ("GET", f"/v1/portfolio/{_ACCOUNT_ID}", {}),
    ("GET", f"/v1/portfolio/{_ACCOUNT_ID}/positions", {}),
    ("GET", f"/v1/portfolio/{_ACCOUNT_ID}/ledger", {}),
    ("GET", "/v1/orders/999999999999", {}),  # 404 mit Error-Envelope
    ("GET", "/v1/trades", {}),
]


def _create_test_token(client: TestClient) -> str:
    """Erzeugt via POST /v1/auth/token einen frischen, ALL_SCOPES-Token.

    Der Token bekommt alle Scopes, damit ein einziger Wert genuegt um
    saemtliche Read-Endpunkte ohne 403 zu erreichen. Test-Disziplin
    laesst sich dadurch in einem Lauf vereinen.
    """
    response = client.post(
        "/v1/auth/token",
        headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        json={
            "caller_id": "leak-scan",
            "scopes": sorted(ALL_SCOPES),
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    value = body["value"]
    assert isinstance(value, str)
    # Mit token_urlsafe(32) erzeugt der Server ~43 Zeichen; Mindestlaenge
    # erzwingen, damit der Substring-Match nicht durch Zufall greift.
    assert len(value) >= 32, value
    return value


def test_token_value_does_not_leak_into_any_read_endpoint(client: TestClient) -> None:
    """Pflicht: Bearer-Token-Wert taucht NIE im Body eines Read-Endpunkts auf.

    Erzeugt einen Token, faehrt ihn gegen jeden Endpunkt aus
    ``_READ_ENDPOINTS``, und prueft pro Antwort:

    1. ``response.text`` enthaelt den Token-Wert NICHT.
    2. Keiner der Response-Header (z.B. ``X-Request-ID``) enthaelt den
       Token-Wert. Headers werden zwar von der Observability-Middleware
       redacted geloggt, aber wenn jemand den Token in einen
       Custom-Header echo'en wuerde, faellt das hier auf.
    """
    token_value = _create_test_token(client)

    failures: list[tuple[str, str, int, str]] = []
    for method, path, params in _READ_ENDPOINTS:
        response = client.request(
            method,
            path,
            params=params or None,
            headers={"Authorization": f"Bearer {token_value}"},
        )
        # Status-Code-Gate ist absichtlich tolerant - ein 404 vom
        # Orders-Endpunkt z.B. ist OK, weil die ID synthetisch ist; ein
        # 503 vom Lifecycle waere ein anderer Fehler. Wichtig ist nur,
        # dass auch in *Fehler*-Antworten der Token nicht im Body steht.
        body_text = response.text
        if token_value in body_text:
            failures.append((method, path, response.status_code, "body"))
        for header_name, header_value in response.headers.items():
            if token_value in header_value:
                failures.append((method, path, response.status_code, f"header:{header_name}"))

    assert not failures, (
        "Token-Leak in Response-Body oder -Header detektiert: "
        + "; ".join(f"{m} {p} -> {s} ({where})" for m, p, s, where in failures)
    )


def test_post_auth_token_is_intentionally_excluded(client: TestClient) -> None:
    """Verifiziert die Karten-Anforderung 1.d: POST /v1/auth/token DARF
    den value zurueckgeben - das ist sein Zweck.

    Wenn der Endpunkt den Wert NICHT mehr zurueckgibt, ist die
    Token-Erzeugung unbrauchbar. Dieser Test schuetzt die Auslassung
    in :data:`_READ_ENDPOINTS` vor unbeabsichtigtem Drift.
    """
    response = client.post(
        "/v1/auth/token",
        headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        json={"caller_id": "create-and-check", "scopes": list(ALL_SCOPES)},
    )
    assert response.status_code == 201
    body = response.json()
    # Genau hier ist der Token-Echo erwuenscht.
    assert body["value"]
    assert body["caller_id"] == "create-and-check"
