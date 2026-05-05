"""Tests fuer POST /v1/admin/auto-login/trigger.

Pruefen: Auth-Schutz, 503 wenn kein Trigger attached, 200 mit
korrekter Response wenn Trigger laeuft.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.middleware import get_token_store
from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.auto_login_trigger import (
    AutoLoginResult,
    TriggerOutcome,
)
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app


_BOOTSTRAP_VALUE = "admin-trigger-token-aaaaaaaaaaaaaaaa"


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
    from broker_gateway.cp.client import CPGatewayClient

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


class _FakeTrigger:
    def __init__(self, outcome: TriggerOutcome) -> None:
        self._outcome = outcome
        self.calls = 0

    async def maybe_trigger(self) -> TriggerOutcome:
        self.calls += 1
        return self._outcome


def _attach_trigger(app, outcome: TriggerOutcome) -> _FakeTrigger:
    trigger = _FakeTrigger(outcome)
    app.state.auto_login_trigger = trigger
    return trigger


# ---- 503 wenn kein Trigger attached ----


def test_trigger_returns_503_when_disabled(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        # In den Tests setzt die conftest BG_PAPER_AUTO_LOGIN nicht,
        # also wurde der Trigger im Lifespan NICHT attached.
        response = client.post(
            "/v1/admin/auto-login/trigger",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "auto_login_disabled"


# ---- Auth-Schutz ----


def test_trigger_requires_admin_token(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        unauth = client.post("/v1/admin/auto-login/trigger")
    assert unauth.status_code == 401


def test_trigger_requires_admin_scope(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Token ohne admin:* darf den Endpoint nicht treffen."""
    from broker_gateway.auth.models import SCOPE_PORTFOLIO_READ

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
            "/v1/admin/auto-login/trigger",
            headers={"Authorization": f"Bearer {no_admin}"},
        )
    assert response.status_code == 403


# ---- Erfolgreicher Trigger-Aufruf ----


def test_trigger_returns_outcome_when_attached(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    outcome = TriggerOutcome(
        skipped=False,
        reason="success",
        result=AutoLoginResult(exit_code=0, duration_s=1.5, error=None),
    )
    fake = _attach_trigger(application, outcome)
    with TestClient(application) as client:
        # Nach dem Lifespan-Start ueberschreiben — der Lifespan haengt
        # ggf. einen echten Trigger an, hier wollen wir den fake.
        application.state.auto_login_trigger = fake
        response = client.post(
            "/v1/admin/auto-login/trigger",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] is False
    assert body["reason"] == "success"
    assert body["exit_code"] == 0
    assert body["duration_s"] == 1.5
    assert body["error"] is None
    assert fake.calls == 1


def test_trigger_reports_skipped_outcome(
    store: InMemoryTokenStore, lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    outcome = TriggerOutcome(skipped=True, reason="cooldown_5min")
    fake = _attach_trigger(application, outcome)
    with TestClient(application) as client:
        application.state.auto_login_trigger = fake
        response = client.post(
            "/v1/admin/auto-login/trigger",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["skipped"] is True
    assert body["reason"] == "cooldown_5min"
    assert body["exit_code"] is None
    assert body["duration_s"] is None
