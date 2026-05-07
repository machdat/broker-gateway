"""Phase F.integration fuer Karte 406fce15: POST /v1/internal/seed-cookies.

Pruefen: Auth-Schutz (401/403), Body-Validation (422), Jar-Befuellung,
sofortiger Tick-Trigger, Services-Client-Sync.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.middleware import get_token_store
from broker_gateway.auth.models import SCOPE_ADMIN_ALL, SCOPE_PORTFOLIO_READ, Token
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app


_BOOTSTRAP_VALUE = "admin-seed-cookies-token-aaaaaaaaaaaa"
_VALID_BODY = {
    "jsessionid": "session-from-browser",
    "x_sess_uuid": "uuid-from-browser",
}


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
async def lifecycle(cp_gateway_mock):
    client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    lc = AuthLifecycle(
        client,
        tickle_interval_s=0.05,
        reauth_max_retries=1,
        reauth_backoff_s=0.0,
        bridge_probe_interval_s=0.05,
        bridge_reauth_warmup_s=0.0,
    )
    yield lc
    await lc.stop()
    await client.aclose()


# ---- Auth-Schutz ----


def test_seed_cookies_requires_token(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post("/v1/internal/seed-cookies", json=_VALID_BODY)
    assert response.status_code == 401


def test_seed_cookies_requires_admin_scope(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    no_admin = "no-admin-token-aaaaaaaaaaaaaaaaaaaa"
    store.put(
        Token(
            value=no_admin,
            caller_id="reader",
            scopes=[SCOPE_PORTFOLIO_READ],
        )
    )
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {no_admin}"},
            json=_VALID_BODY,
        )
    assert response.status_code == 403


# ---- Body-Validation ----


def test_seed_cookies_rejects_missing_jsessionid(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            json={"x_sess_uuid": "uuid"},
        )
    assert response.status_code == 422


def test_seed_cookies_rejects_empty_jsessionid(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            json={"jsessionid": "", "x_sess_uuid": "uuid"},
        )
    assert response.status_code == 422


def test_seed_cookies_rejects_missing_x_sess_uuid(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            json={"jsessionid": "session"},
        )
    assert response.status_code == 422


# ---- Erfolgreiches Seeding ----


def test_seed_cookies_populates_lifecycle_client_jar(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            json=_VALID_BODY,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["seeded"] == ["JSESSIONID", "x-sess-uuid"]
    assert body["tick_triggered"] is True

    jar = lifecycle.client.cookies
    assert jar.get("JSESSIONID") == "session-from-browser"
    assert jar.get("x-sess-uuid") == "uuid-from-browser"


def test_seed_cookies_returns_current_auth_status(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Nach dem Seed + Tick liefert der Mock authenticated=True ->
    auth_status sollte ok sein, weil der Mock-Tickle die Phase-2.2-
    Pfade durchlaeuft.
    """
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            json=_VALID_BODY,
        )
    assert response.status_code == 200
    body = response.json()
    # ok, weil cp_gateway_mock einen authenticated tickle liefert.
    assert body["auth_status"] == "ok"


def test_seed_cookies_triggers_immediate_tick(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Der Endpoint setzt erst die Cookies und ruft dann tick_once auf.

    Eindeutiger Beweis fuer den Trigger ist der ``tick_triggered``-Flag
    in der Response: das Feld wird ausschliesslich im seed-cookies-
    Pfad gesetzt (nicht vom Hintergrund-Loop). Zusatzcheck: nach dem
    Aufruf hat die Snapshot einen ``last_tickle_at``-Timestamp — er
    koennte zwar auch vom Loop stammen (tickle_interval_s=0.05), aber
    seine Existenz ist Voraussetzung fuer den Phase-C-Erfolgspfad.
    """
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            json=_VALID_BODY,
        )
    assert response.status_code == 200
    assert response.json()["tick_triggered"] is True

    snap = lifecycle.snapshot()
    assert snap.last_tickle_at is not None


# ---- Services-Client-Sync ----


# ---- Phase D: ssodh/init-Trigger ----


def test_seed_cookies_triggers_ssodh_init_by_default(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """ssodh_init defaultet auf True — der Mock muss genau einen
    /iserver/auth/ssodh/init-Aufruf mit body {keepAlive: true} sehen.
    """
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            json=_VALID_BODY,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ssodh_init_status"] == "ok"
    assert cp_gateway_mock.ssodh_init_calls == [{"keepAlive": True}]


def test_seed_cookies_skips_ssodh_init_when_disabled(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """ssodh_init=False -> kein Mock-Aufruf, status=skipped."""
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            json={**_VALID_BODY, "ssodh_init": False},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ssodh_init_status"] == "skipped"
    assert cp_gateway_mock.ssodh_init_calls == []


def test_seed_cookies_handles_ssodh_init_error(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Wenn cpgateway 503 zurueckgibt, bleibt der Endpoint 200 und
    setzt ssodh_init_status=error. Phase-B-Path-Override ist Fallback.
    """
    cp_gateway_mock.ssodh_init_should_fail = True
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            json=_VALID_BODY,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["ssodh_init_status"] == "error"
    # Cookies trotzdem geseedet — die Hauptfunktion hat geklappt.
    assert lifecycle.client.cookies.get("JSESSIONID") == "session-from-browser"


def test_seed_cookies_does_not_seed_services_client_when_shared(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Wenn der Services-Client identisch zum Lifecycle-Client ist
    (Default-Production), wird kein zweiter Seed durchgefuehrt — sonst
    haetten wir einen doppelten Cookie-Set (idempotent zwar, aber
    seeded_in_services_client sollte False sein).

    Im Test-Setup mit injiziertem Lifecycle ist der Services-Client
    aber ein separater — der Endpoint sollte beide befuellen.
    """
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.post(
            "/v1/internal/seed-cookies",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
            json=_VALID_BODY,
        )
    assert response.status_code == 200
    body = response.json()
    # Im Test-Setup wird ein separater services_client angelegt, weil
    # `lifecycle is not None`. Der Service-Client landet in
    # app.state._services_client und ist != lifecycle.client.
    assert body["seeded_in_services_client"] is True

    services_client = application.state._services_client
    assert services_client is not lifecycle.client
    assert services_client.cookies.get("JSESSIONID") == "session-from-browser"
    assert services_client.cookies.get("x-sess-uuid") == "uuid-from-browser"
