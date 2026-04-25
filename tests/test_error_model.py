"""Tests fuer das vereinheitlichte Error-Modell (v1-Section 1.6).

Verifiziert: jede Fehlerantwort folgt dem Schema

    { "error": { "code": "...", "message": "...",
                 "request_id": "...", "retry_after_s": 30,
                 "extra": { ... } } }

Pflicht-Cases laut Karte AP-02 #05:
  - missing_token / invalid_token (401)
  - missing_scope (403)
  - not_found (404)
  - invalid_input (422)
  - cp_pacing_violation (429 mit Retry-After)
  - auth_lost (503 mit Retry-After)
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker_gateway.api.v1.errors import KNOWN_ERROR_CODES
from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_PORTFOLIO_READ,
    SCOPE_QUOTES_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore, generate_token_value
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle, AuthStatus
from broker_gateway.main import create_app


_ADMIN_VALUE = generate_token_value()
_QUOTES_VALUE = generate_token_value()


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(Token(value=_ADMIN_VALUE, caller_id="admin", scopes=[SCOPE_ADMIN_ALL]))
    s.put(Token(value=_QUOTES_VALUE, caller_id="psm", scopes=[SCOPE_QUOTES_READ]))
    return s


@pytest.fixture
async def cp_client(cp_gateway_mock) -> CPGatewayClient:
    cl = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    yield cl
    await cl.aclose()


@pytest.fixture
async def lifecycle(cp_client: CPGatewayClient) -> AuthLifecycle:
    lc = AuthLifecycle(cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0)
    yield lc
    await lc.stop()


@pytest.fixture
def client(store, lifecycle, cp_gateway_mock):
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as tc:
        yield tc


def _assert_envelope(body: dict, *, code: str, status_code: int) -> dict:
    assert "error" in body, f"missing 'error'-key in body: {body}"
    err = body["error"]
    assert isinstance(err, dict), f"error is not a dict: {err}"
    assert "code" in err and "message" in err
    assert err["code"] in KNOWN_ERROR_CODES, f"unknown code {err['code']!r}"
    assert err["code"] == code
    assert isinstance(err["message"], str) and err["message"]
    assert "detail" not in body, "old FastAPI 'detail' leaked into response"
    return err


# ---- Pflicht-Case: missing_token (401) ----

def test_missing_token_returns_401_missing_token(client: TestClient) -> None:
    response = client.get("/v1/portfolio/U25235077")
    assert response.status_code == 401
    err = _assert_envelope(response.json(), code="missing_token", status_code=401)
    assert "Authorization" in err["message"] or "fehlt" in err["message"]


# ---- Pflicht-Case: invalid_token (401) ----

def test_unknown_token_returns_401_invalid_token(client: TestClient) -> None:
    response = client.get(
        "/v1/portfolio/U25235077",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401
    _assert_envelope(response.json(), code="invalid_token", status_code=401)


# ---- Pflicht-Case: missing_scope (403) ----

def test_missing_scope_returns_403_with_required_scope(client: TestClient) -> None:
    response = client.get(
        "/v1/portfolio/U25235077",
        headers={"Authorization": f"Bearer {_QUOTES_VALUE}"},
    )
    assert response.status_code == 403
    err = _assert_envelope(response.json(), code="missing_scope", status_code=403)
    assert err.get("extra", {}).get("required_scope") == SCOPE_PORTFOLIO_READ


# ---- Pflicht-Case: invalid_input (422) ----

def test_validation_error_returns_422_invalid_input(client: TestClient) -> None:
    response = client.get(
        "/v1/quotes/snapshot",
        params={"conids": ""},
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 422
    _assert_envelope(response.json(), code="invalid_input", status_code=422)


# ---- Pflicht-Case: not_found (404) ----

def test_unknown_route_returns_404_not_found(client: TestClient) -> None:
    response = client.get(
        "/v1/this/does/not/exist",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 404
    _assert_envelope(response.json(), code="not_found", status_code=404)


# ---- Pflicht-Case: cp_pacing_violation (429 mit Retry-After) ----

def test_pacing_violation_returns_429_cp_pacing_violation(
    client: TestClient, cp_gateway_mock
) -> None:
    cp_gateway_mock.pacing_violation_after_n = 0
    response = client.get(
        "/v1/quotes/snapshot",
        params={"conids": "265598"},
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 429
    err = _assert_envelope(response.json(), code="cp_pacing_violation", status_code=429)
    # Retry-After kommt entweder als Header oder im error.retry_after_s.
    assert (
        response.headers.get("Retry-After")
        or err.get("retry_after_s") is not None
    ), "weder Retry-After-Header noch retry_after_s im Body gesetzt"


# ---- Pflicht-Case: auth_lost (503 mit Retry-After) ----

def test_auth_lost_returns_503_auth_lost_with_retry_after(
    client: TestClient, cp_gateway_mock, lifecycle
) -> None:
    cp_gateway_mock.auth_lost = True
    # Lifecycle-Snapshot liest self._status; wir setzen den Status manuell,
    # damit der naechste Request 503 sieht ohne auf einen Tickle zu warten.
    lifecycle._status = AuthStatus.AUTH_LOST  # noqa: SLF001 - Test-Hilfe

    response = client.get(
        "/v1/portfolio/U25235077",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 503
    err = _assert_envelope(response.json(), code="auth_lost", status_code=503)
    assert response.headers.get("Retry-After"), "Retry-After-Header fehlt"
    assert err.get("retry_after_s") is not None


# ---- Default-Code-Mapping fuer alle bekannten Stati ----

@pytest.mark.parametrize(
    "status_code,expected_code",
    [
        (400, "invalid_input"),
        (401, "missing_token"),
        (403, "missing_scope"),
        (404, "not_found"),
        (422, "invalid_input"),
        (429, "cp_pacing_violation"),
        (503, "auth_lost"),
    ],
)
def test_default_code_for_known_statuses(status_code: int, expected_code: str) -> None:
    from broker_gateway.api.v1.errors import default_code_for

    assert default_code_for(status_code) == expected_code
