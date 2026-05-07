"""Tests fuer CPGatewayClient + AuthLifecycle + /v1/internal/health."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterator

import httpx
import pytest
import respx
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from broker_gateway.auth.middleware import get_token_store
from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_PORTFOLIO_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import (
    AuthLifecycle,
    AuthStatus,
    require_session_ok,
)
from broker_gateway.main import create_app


_BOOTSTRAP_VALUE = "lifecycle-admin-token-aaaaaaaaaaaaaaaa"


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
        tickle_interval_s=0.05,
        reauth_max_retries=3,
        reauth_backoff_s=0.0,
        # Bridge-Probe in Tests aggressiver konfigurieren als in
        # Production: kurzes Intervall fuer Loop-Probe-Tests, kein Warmup.
        bridge_probe_interval_s=0.05,
        bridge_reauth_warmup_s=0.0,
    )
    yield lc
    await lc.stop()


# ---- CPGatewayClient ----

async def test_client_uses_env_base_url_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BG_CP_BASE_URL", raising=False)
    client = CPGatewayClient()
    assert client.base_url == "http://cpgateway:5000/v1/api"
    await client.aclose()


async def test_client_reads_env_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_CP_BASE_URL", "http://example:1234/")
    client = CPGatewayClient()
    assert client.base_url == "http://example:1234"
    await client.aclose()


async def test_client_pacing_hook_called_for_each_request(cp_gateway_mock) -> None:
    calls: list[tuple[str, str]] = []

    async def hook(method: str, path: str) -> None:
        calls.append((method, path))

    client = CPGatewayClient(base_url=cp_gateway_mock.base_url, pacing_hook=hook)
    try:
        await client.tickle()
        await client.auth_status()
    finally:
        await client.aclose()
    assert ("POST", "/tickle") in calls
    assert ("GET", "/iserver/auth/status") in calls


# ---- AuthLifecycle - tick_once ----

async def test_first_tick_sets_status_ok(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    status = await lifecycle.tick_once()
    assert status is AuthStatus.OK
    snap = lifecycle.snapshot()
    assert snap.cp_reachable is True
    assert snap.last_tickle_at is not None
    assert snap.consecutive_reauth_failures == 0


async def test_auth_loss_triggers_reauth_then_recovers(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    cp_gateway_mock.auth_lost = True

    async def restore_after_first_reauth(method: str, path: str) -> None:
        if path == "/reauthenticate":
            cp_gateway_mock.auth_lost = False

    lifecycle._client._pacing = restore_after_first_reauth  # type: ignore[attr-defined]

    status = await lifecycle.tick_once()
    assert status is AuthStatus.OK
    snap = lifecycle.snapshot()
    assert snap.last_reauth_at is not None
    assert snap.consecutive_reauth_failures == 0


async def test_persistent_auth_loss_ends_in_auth_lost(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    cp_gateway_mock.auth_lost = True
    status = await lifecycle.tick_once()
    assert status is AuthStatus.AUTH_LOST
    snap = lifecycle.snapshot()
    assert snap.consecutive_reauth_failures == lifecycle.reauth_max_retries
    assert snap.last_reauth_at is not None


async def test_cp_unreachable_marks_cp_down() -> None:
    # Echter Client gegen einen unerreichbaren Host (kein respx aktiv).
    client = CPGatewayClient(base_url="http://127.0.0.1:1")
    try:
        lc = AuthLifecycle(client, tickle_interval_s=0.05, reauth_max_retries=1, reauth_backoff_s=0.0)
        status = await lc.tick_once()
        assert status is AuthStatus.CP_DOWN
        assert lc.snapshot().cp_reachable is False
    finally:
        await client.aclose()


async def test_tickle_401_keeps_cp_reachable_and_marks_auth_lost() -> None:
    """Karte 739777a9: 401 von cpgateway = Session unauthenticated,
    NICHT CP-down. cpgateway antwortet ja, also bleibt cp_reachable=True
    und last_tickle_at darf aktualisiert werden. Status wandert in den
    Reauth-Loop / AUTH_LOST, nicht CP_DOWN.
    """
    base_url = "http://cpgateway-401-test:5000/v1/api"
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=rf"^{base_url}/sso/validate$").respond(
            status_code=401, json={}
        )
        router.post(url__regex=rf"^{base_url}/tickle$").respond(
            status_code=401, json={}
        )
        router.post(url__regex=rf"^{base_url}/reauthenticate$").respond(
            status_code=401, json={}
        )

        client = CPGatewayClient(base_url=base_url)
        try:
            lc = AuthLifecycle(
                client,
                tickle_interval_s=0.05,
                reauth_max_retries=1,
                reauth_backoff_s=0.0,
            )
            status = await lc.tick_once()
            snap = lc.snapshot()
            assert snap.cp_reachable is True, (
                "cpgateway hat mit 401 geantwortet -> reachable bleibt True"
            )
            assert snap.last_tickle_at is not None, (
                "Tickle hat eine Antwort bekommen (auch wenn 401) -> last_tickle_at gesetzt"
            )
            assert status is not AuthStatus.CP_DOWN, (
                "401 ist nicht CP-Down, sondern Auth-Loss"
            )
            # Reauth wurde mindestens einmal versucht, ist aber gescheitert
            assert snap.last_reauth_at is not None
        finally:
            await client.aclose()


# ---- AuthLifecycle - sso/validate, accounts-Init, force-reauth ----


async def test_first_tick_calls_iserver_accounts_init(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Karte AP-02 #07-3: erster Tickle nach Login muss GET
    /iserver/accounts ausloesen."""
    calls: list[tuple[str, str]] = []

    async def hook(method: str, path: str) -> None:
        calls.append((method, path))

    lifecycle._client._pacing = hook  # type: ignore[attr-defined]
    await lifecycle.tick_once()
    assert ("GET", "/iserver/accounts") in calls
    assert lifecycle.snapshot().accounts_initialized is True


async def test_accounts_init_only_once(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Folge-Tickles wiederholen den /iserver/accounts-Call nicht."""
    calls: list[tuple[str, str]] = []

    async def hook(method: str, path: str) -> None:
        calls.append((method, path))

    lifecycle._client._pacing = hook  # type: ignore[attr-defined]
    await lifecycle.tick_once()
    await lifecycle.tick_once()
    accounts_calls = [c for c in calls if c == ("GET", "/iserver/accounts")]
    assert len(accounts_calls) == 1


async def test_tick_calls_sso_validate_as_primary_keepalive(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Karte AP-02 #07-3: Keep-Alive geht primaer ueber GET /sso/validate."""
    calls: list[tuple[str, str]] = []

    async def hook(method: str, path: str) -> None:
        calls.append((method, path))

    lifecycle._client._pacing = hook  # type: ignore[attr-defined]
    await lifecycle.tick_once()
    assert ("GET", "/sso/validate") in calls
    snap = lifecycle.snapshot()
    assert snap.last_sso_validate_at is not None


async def test_first_tick_sets_last_login_at(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Erster Uebergang in OK setzt last_login_at."""
    before = datetime.now(timezone.utc)
    await lifecycle.tick_once()
    snap = lifecycle.snapshot()
    assert snap.last_login_at is not None
    assert snap.last_login_at >= before


async def test_force_reauth_triggers_reauthenticate(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """reauthenticate(force=True) ruft POST /iserver/reauthenticate
    auch wenn der lokale Status OK ist."""
    await lifecycle.tick_once()
    assert lifecycle.status is AuthStatus.OK

    calls: list[tuple[str, str]] = []

    async def hook(method: str, path: str) -> None:
        calls.append((method, path))

    lifecycle._client._pacing = hook  # type: ignore[attr-defined]
    result = await lifecycle.reauthenticate(force=True)
    assert ("POST", "/reauthenticate") in calls
    assert result is AuthStatus.OK
    assert lifecycle.snapshot().last_reauth_at is not None


async def test_default_reauthenticate_no_op_when_status_ok(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """reauthenticate() ohne force tut nichts, wenn der Status OK ist."""
    await lifecycle.tick_once()
    assert lifecycle.status is AuthStatus.OK

    calls: list[tuple[str, str]] = []

    async def hook(method: str, path: str) -> None:
        calls.append((method, path))

    lifecycle._client._pacing = hook  # type: ignore[attr-defined]
    result = await lifecycle.reauthenticate()
    assert result is AuthStatus.OK
    assert ("POST", "/reauthenticate") not in calls


# ---- AuthLifecycle - background loop ----

async def test_background_loop_increments_tickle_counter(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    await lifecycle.start()
    # tick_once aus start() ist bereits durch; warte auf zwei weitere Zyklen.
    await asyncio.sleep(lifecycle.tickle_interval_s * 3.5)
    await lifecycle.stop()
    # Der Mock zaehlt jeden Request. Wir erwarten mindestens drei /tickle-Calls
    # (initialer + zwei Loop-Tickles).
    assert cp_gateway_mock.request_count >= 3


# ---- /v1/internal/health ----

async def test_internal_health_requires_admin(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        no_token = client.get("/v1/internal/health")
        assert no_token.status_code == 401

        admin = client.get(
            "/v1/internal/health",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert admin.status_code == 200
    body = admin.json()
    assert body["auth_status"] == "ok"
    assert body["cp_reachable"] is True
    assert body["consecutive_reauth_failures"] == 0
    assert body["last_tickle_at"] is not None
    assert body["last_sso_validate_at"] is not None
    assert body["last_login_at"] is not None
    assert body["accounts_initialized"] is True


async def test_internal_health_reports_auth_lost(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    cp_gateway_mock.auth_lost = True
    await lifecycle.tick_once()
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.get(
            "/v1/internal/health",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert response.status_code == 200
    assert response.json()["auth_status"] == "auth_lost"


# ---- require_session_ok ----

async def test_require_session_ok_returns_503_with_retry_after(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    cp_gateway_mock.auth_lost = True
    await lifecycle.tick_once()  # Status -> AUTH_LOST nach max-retries

    application = create_app(store=store, lifecycle=lifecycle)

    @application.get("/test/quote")
    def _quote(_lc=Depends(require_session_ok)) -> dict[str, str]:
        return {"price": "150.00"}

    with TestClient(application) as client:
        response = client.get(
            "/test/quote",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert response.status_code == 503
    assert response.headers.get("retry-after") == "30"


async def test_require_session_ok_passes_when_status_ok(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)

    @application.get("/test/quote")
    def _quote(_lc=Depends(require_session_ok)) -> dict[str, str]:
        return {"price": "150.00"}

    with TestClient(application) as client:
        response = client.get(
            "/test/quote",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert response.status_code == 200


# ---- iserver-Bridge-Probe (Karte 81e3c818) ----


async def test_bridge_probe_marks_bridge_ok_on_healthy_response(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Gesundes /iserver/auth/status (Trio alle true) -> bridge_ok=True."""
    result = await lifecycle.bridge_probe_once()
    assert result is True
    snap = lifecycle.snapshot()
    assert snap.iserver_bridge_ok is True
    assert snap.last_bridge_probe_at is not None
    assert snap.consecutive_bridge_failures == 0


async def test_bridge_probe_triggers_recovery_on_drift(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Drift -> einmaliger reauth via Hook geheilt -> bridge_ok zurueck auf True."""
    cp_gateway_mock.bridge_drift = True

    async def heal_on_reauth(method: str, path: str) -> None:
        if path == "/reauthenticate":
            cp_gateway_mock.bridge_drift = False

    lifecycle._client._pacing = heal_on_reauth  # type: ignore[attr-defined]

    result = await lifecycle.bridge_probe_once()
    assert result is True
    snap = lifecycle.snapshot()
    assert snap.iserver_bridge_ok is True
    assert snap.consecutive_bridge_failures == 0
    assert snap.last_reauth_at is not None
    assert lifecycle.status is AuthStatus.OK


async def test_bridge_probe_three_drifts_escalate_to_auth_lost(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Persistenter Drift: 3x Probe ohne Recovery -> auth_status=AUTH_LOST."""
    cp_gateway_mock.bridge_drift = True
    for expected_failures in (1, 2, 3):
        result = await lifecycle.bridge_probe_once()
        assert result is False
        snap = lifecycle.snapshot()
        assert snap.iserver_bridge_ok is False
        assert snap.consecutive_bridge_failures == expected_failures
    assert lifecycle.status is AuthStatus.AUTH_LOST


async def test_bridge_probe_returns_none_when_call_raises(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Network-Fehler beim Probe -> bisheriger Bridge-State unveraendert."""
    # Erst einen erfolgreichen Probe, damit bridge_ok=True gesetzt ist.
    await lifecycle.bridge_probe_once()
    assert lifecycle.snapshot().iserver_bridge_ok is True

    original = lifecycle._client.auth_status

    async def fail_once() -> dict:
        raise httpx.ConnectError("simulated network failure")

    lifecycle._client.auth_status = fail_once  # type: ignore[assignment]
    try:
        result = await lifecycle.bridge_probe_once()
    finally:
        lifecycle._client.auth_status = original  # type: ignore[assignment]
    # Kein State-Change: bridge_ok bleibt True, kein Failure-Increment.
    assert result is None
    snap = lifecycle.snapshot()
    assert snap.iserver_bridge_ok is True
    assert snap.consecutive_bridge_failures == 0


async def test_background_loop_runs_bridge_probe(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Hintergrund-Loop ruft bridge_probe_once mit eigenem Intervall."""
    await lifecycle.start()
    # Initialer Probe ist schon durch (im start()), warte auf mindestens
    # einen weiteren Loop-Tick mit Bridge-Probe.
    await asyncio.sleep(lifecycle.tickle_interval_s * 3.5)
    await lifecycle.stop()
    snap = lifecycle.snapshot()
    assert snap.iserver_bridge_ok is True
    assert snap.last_bridge_probe_at is not None


async def test_bridge_probe_skipped_in_initial_start_when_auth_lost(
    cp_client: CPGatewayClient, cp_gateway_mock
) -> None:
    """Wenn der erste Tickle in AUTH_LOST endet, soll start() den initialen
    Bridge-Probe ueberspringen (sonst zaehlt der Counter unnoetig hoch)."""
    cp_gateway_mock.auth_lost = True
    lc = AuthLifecycle(
        cp_client,
        tickle_interval_s=10.0,
        reauth_max_retries=1,
        reauth_backoff_s=0.0,
        bridge_probe_interval_s=10.0,
        bridge_reauth_warmup_s=0.0,
    )
    try:
        await lc.start()
    finally:
        await lc.stop()
    snap = lc.snapshot()
    assert lc.status is AuthStatus.AUTH_LOST
    # Kein initialer Probe -> bridge_ok bleibt None
    assert snap.iserver_bridge_ok is None
    assert snap.last_bridge_probe_at is None


async def test_internal_health_includes_bridge_fields(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    """/v1/internal/health spiegelt iserver_bridge_ok, last_bridge_probe_at,
    consecutive_bridge_failures aus dem Snapshot."""
    await lifecycle.tick_once()
    await lifecycle.bridge_probe_once()
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.get(
            "/v1/internal/health",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["iserver_bridge_ok"] is True
    assert body["last_bridge_probe_at"] is not None
    assert body["consecutive_bridge_failures"] == 0


async def test_internal_health_includes_auto_login_fields(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    """/v1/internal/health spiegelt die Auto-Login-Felder.

    Default-Werte (kein Versuch gelaufen): None / 0 / "ready" — auch
    im Live-Stack, wo Auto-Login nie aktiv ist.
    """
    await lifecycle.tick_once()
    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.get(
            "/v1/internal/health",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["last_auto_login_attempt_at"] is None
    assert body["last_auto_login_success_at"] is None
    assert body["auto_login_failures_total"] == 0
    assert body["auto_login_throttle_state"] == "ready"


async def test_internal_health_reflects_auto_login_state(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    """Nach update_auto_login muss /v1/internal/health die neuen Werte zeigen."""
    from datetime import datetime, timezone

    await lifecycle.tick_once()
    now = datetime(2026, 5, 5, 14, 30, 0, tzinfo=timezone.utc)
    lifecycle.update_auto_login(
        attempted_at=now,
        succeeded_at=now,
        throttle_state="cooldown_5min",
    )
    lifecycle.update_auto_login(failure_increment=2, throttle_state="cooldown_15min")

    application = create_app(store=store, lifecycle=lifecycle)
    with TestClient(application) as client:
        response = client.get(
            "/v1/internal/health",
            headers={"Authorization": f"Bearer {_BOOTSTRAP_VALUE}"},
        )
    body = response.json()
    assert body["last_auto_login_attempt_at"] is not None
    assert body["last_auto_login_success_at"] is not None
    assert body["auto_login_failures_total"] == 2
    assert body["auto_login_throttle_state"] == "cooldown_15min"


# ---- Auto-Login-Verdrahtung in den Tickle-Loop ----


class _RecordingTrigger:
    def __init__(self) -> None:
        self.calls = 0

    async def maybe_trigger(self):
        self.calls += 1
        from broker_gateway.cp.auto_login_trigger import TriggerOutcome

        return TriggerOutcome(skipped=True, reason="ok-noop-test")


async def test_auto_login_trigger_invoked_on_auth_lost(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    trigger = _RecordingTrigger()
    lifecycle.attach_auto_login_trigger(trigger)
    cp_gateway_mock.auth_lost = True
    await lifecycle.tick_once()  # Status wird auf AUTH_LOST gehen
    assert lifecycle.status is AuthStatus.AUTH_LOST
    assert trigger.calls == 1


async def test_auto_login_trigger_not_invoked_on_ok(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    trigger = _RecordingTrigger()
    lifecycle.attach_auto_login_trigger(trigger)
    await lifecycle.tick_once()
    assert lifecycle.status is AuthStatus.OK
    assert trigger.calls == 0


async def test_auto_login_trigger_exception_does_not_break_lifecycle(
    lifecycle: AuthLifecycle, cp_gateway_mock
) -> None:
    """Wenn der Trigger eine Exception wirft, muss tick_once trotzdem
    sauber zurueckkehren — der Tickle-Loop darf nicht stehen bleiben."""

    class _ExplodingTrigger:
        async def maybe_trigger(self):
            raise RuntimeError("boom")

    lifecycle.attach_auto_login_trigger(_ExplodingTrigger())
    cp_gateway_mock.auth_lost = True
    # Darf NICHT raisen.
    await lifecycle.tick_once()
    assert lifecycle.status is AuthStatus.AUTH_LOST
