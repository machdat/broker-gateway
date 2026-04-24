"""Tests fuer Auth-Modell, Store, Middleware und /v1/auth-Endpunkte."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from broker_gateway.api.v1 import router as v1_router
from broker_gateway.auth.middleware import (
    get_token_store,
    require_scope,
)
from broker_gateway.auth.models import (
    ALL_SCOPES,
    SCOPE_ADMIN_ALL,
    SCOPE_ORDERS_WRITE,
    SCOPE_PORTFOLIO_READ,
    SCOPE_QUOTES_READ,
    Token,
    TokenView,
)
from broker_gateway.auth.store import (
    FileTokenStore,
    InMemoryTokenStore,
    build_default_store,
    generate_token_value,
)
from broker_gateway.main import create_app


_BOOTSTRAP_VALUE = "bootstrap-admin-token-1234567890"


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
def app(store: InMemoryTokenStore) -> FastAPI:
    application = create_app(store=store)
    # Test-Endpunkt fuer Scope-Pruefung (positive/negative).
    @application.get("/test/portfolio")
    def _portfolio(_t: Token = Depends(require_scope(SCOPE_PORTFOLIO_READ))) -> dict[str, str]:
        return {"ok": "true"}

    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


# ---------- Modelle ----------

def test_all_scopes_set_contains_known_constants() -> None:
    assert SCOPE_QUOTES_READ in ALL_SCOPES
    assert SCOPE_ADMIN_ALL in ALL_SCOPES
    assert SCOPE_ORDERS_WRITE in ALL_SCOPES


def test_token_rejects_unknown_scopes() -> None:
    with pytest.raises(ValueError):
        Token(value="x" * 16, caller_id="psm", scopes=["unknown:scope"])


def test_admin_all_matches_any_required_scope() -> None:
    token = Token(value="x" * 16, caller_id="ops", scopes=[SCOPE_ADMIN_ALL])
    assert token.has_scope(SCOPE_ORDERS_WRITE) is True
    assert token.has_scope(SCOPE_PORTFOLIO_READ) is True


def test_explicit_scope_matches_only_itself() -> None:
    token = Token(value="x" * 16, caller_id="psm", scopes=[SCOPE_QUOTES_READ])
    assert token.has_scope(SCOPE_QUOTES_READ) is True
    assert token.has_scope(SCOPE_ORDERS_WRITE) is False


def test_token_expiry_marks_expired() -> None:
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    token = Token(value="x" * 16, caller_id="psm", scopes=[SCOPE_QUOTES_READ], expires_at=past)
    assert token.is_expired() is True


# ---------- Store ----------

def test_in_memory_store_round_trip() -> None:
    s = InMemoryTokenStore()
    t = Token(value=generate_token_value(), caller_id="psm", scopes=[SCOPE_QUOTES_READ])
    s.put(t)
    assert s.get(t.value) is not None
    assert s.delete(t.value) is True
    assert s.get(t.value) is None


def test_file_store_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "tokens.json"
    a = FileTokenStore(path)
    t = Token(value=generate_token_value(), caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ])
    a.put(t)

    b = FileTokenStore(path)
    loaded = b.get(t.value)
    assert loaded is not None
    assert loaded.scopes == [SCOPE_PORTFOLIO_READ]
    assert loaded.caller_id == "psm"


def test_build_default_store_reads_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "tokens.json"
    monkeypatch.setenv("BG_TOKEN_FILE", str(path))
    s = build_default_store()
    assert isinstance(s, FileTokenStore)
    monkeypatch.delenv("BG_TOKEN_FILE", raising=False)
    assert isinstance(build_default_store(), InMemoryTokenStore)


# ---------- Endpunkte ----------

def test_health_remains_unauthenticated(client: TestClient) -> None:
    response = client.get("/v1/health")
    assert response.status_code == 200


def test_protected_endpoint_without_token_returns_401(client: TestClient) -> None:
    response = client.get("/test/portfolio")
    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_protected_endpoint_with_wrong_scope_returns_403(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    token_value = generate_token_value()
    store.put(Token(value=token_value, caller_id="psm", scopes=[SCOPE_QUOTES_READ]))

    response = client.get(
        "/test/portfolio",
        headers={"Authorization": f"Bearer {token_value}"},
    )
    assert response.status_code == 403


def test_protected_endpoint_with_matching_scope_returns_200(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    token_value = generate_token_value()
    store.put(Token(value=token_value, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))

    response = client.get(
        "/test/portfolio",
        headers={"Authorization": f"Bearer {token_value}"},
    )
    assert response.status_code == 200


def test_admin_token_passes_any_scope_check(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    response = client.get(
        "/test/portfolio",
        headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
    )
    assert response.status_code == 200


def test_expired_token_is_rejected(client: TestClient, store: InMemoryTokenStore) -> None:
    expired_value = generate_token_value()
    store.put(
        Token(
            value=expired_value,
            caller_id="psm",
            scopes=[SCOPE_PORTFOLIO_READ],
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    response = client.get(
        "/test/portfolio",
        headers={"Authorization": f"Bearer {expired_value}"},
    )
    assert response.status_code == 401
    assert "abgelaufen" in response.json()["detail"]


def test_create_token_with_admin_returns_token_view(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/token",
        headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        json={"caller_id": "psm", "scopes": [SCOPE_QUOTES_READ, SCOPE_PORTFOLIO_READ]},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["caller_id"] == "psm"
    assert sorted(body["scopes"]) == sorted([SCOPE_QUOTES_READ, SCOPE_PORTFOLIO_READ])
    assert len(body["value"]) >= 16


def test_create_token_without_admin_returns_403(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    non_admin = generate_token_value()
    store.put(Token(value=non_admin, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))
    response = client.post(
        "/v1/auth/token",
        headers={"Authorization": f"Bearer {non_admin}"},
        json={"caller_id": "evil", "scopes": [SCOPE_ADMIN_ALL]},
    )
    assert response.status_code == 403


def test_create_token_rejects_unknown_scope(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/token",
        headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        json={"caller_id": "psm", "scopes": ["does:not:exist"]},
    )
    assert response.status_code == 422


def test_revoke_self_invalidates_token(client: TestClient, store: InMemoryTokenStore) -> None:
    target = generate_token_value()
    store.put(Token(value=target, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))

    revoke = client.delete(
        "/v1/auth/token",
        headers={"Authorization": f"Bearer {target}"},
    )
    assert revoke.status_code == 204

    follow_up = client.get(
        "/test/portfolio",
        headers={"Authorization": f"Bearer {target}"},
    )
    assert follow_up.status_code == 401


def test_revoke_foreign_token_requires_admin(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    a = generate_token_value()
    b = generate_token_value()
    store.put(Token(value=a, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))
    store.put(Token(value=b, caller_id="robot", scopes=[SCOPE_ORDERS_WRITE]))

    # a versucht b zu revoken -> 403
    response = client.request(
        "DELETE",
        "/v1/auth/token",
        headers={"Authorization": f"Bearer {a}"},
        params={"value": b},
    )
    assert response.status_code == 403

    # admin darf b revoken -> 204
    response = client.request(
        "DELETE",
        "/v1/auth/token",
        headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        params={"value": b},
    )
    assert response.status_code == 204
    assert store.get(b) is None


def test_token_view_strips_internal_state() -> None:
    token = Token(value=generate_token_value(), caller_id="psm", scopes=[SCOPE_QUOTES_READ])
    view = TokenView.from_token(token)
    assert view.value == token.value
    assert view.scopes == [SCOPE_QUOTES_READ]


def test_bootstrap_admin_loaded_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    bootstrap = "env-admin-token-aaaaaaaaaaaaaaaa"
    monkeypatch.setenv("BG_BOOTSTRAP_ADMIN_TOKEN", bootstrap)
    monkeypatch.delenv("BG_TOKEN_FILE", raising=False)
    application = create_app()
    test_client = TestClient(application)
    response = test_client.post(
        "/v1/auth/token",
        headers={"Authorization": f"Bearer {bootstrap}"},
        json={"caller_id": "psm", "scopes": [SCOPE_QUOTES_READ]},
    )
    assert response.status_code == 201


def test_malformed_authorization_header_returns_401(client: TestClient) -> None:
    for header in ["", "Bearer", "Basic abc", "Bearer "]:
        response = client.get("/test/portfolio", headers={"Authorization": header})
        assert response.status_code == 401, header
