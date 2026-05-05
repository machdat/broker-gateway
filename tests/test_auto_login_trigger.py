"""Tests fuer AutoLoginTrigger (Phase A: Skeleton ohne Sidecar).

Der Trigger entscheidet, ob ein Auto-Login angestossen wird. Phase A
prueft alle Pre-Conditions (Hard-Guards + Throttle) und ruft einen
injizierten ``AutoLoginRunner`` auf. Der echte Sidecar-Aufruf ist
Phase B und wird hier mit einem Mock simuliert.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from broker_gateway.cp.auto_login_throttle import (
    AutoLoginThrottle,
    AutoLoginThrottleConfig,
)
from broker_gateway.cp.auto_login_trigger import (
    AutoLoginResult,
    AutoLoginTrigger,
    TriggerOutcome,
)
from broker_gateway.cp.lifecycle import AuthLifecycle, AuthStatus
from broker_gateway.cp.client import CPGatewayClient


class _StaticLifecycle:
    """Minimaler Stand-In fuer AuthLifecycle in den Trigger-Tests.

    Echte AuthLifecycle-Instanz wuerde ein Tickle-Loop starten, was
    fuer das Pre-Conditions-Verhalten irrelevant ist."""

    def __init__(self, status: AuthStatus = AuthStatus.OK) -> None:
        self._status = status
        self.auto_login_calls: list[dict[str, Any]] = []

    def set_status(self, status: AuthStatus) -> None:
        self._status = status

    def snapshot(self):
        from broker_gateway.cp.lifecycle import LifecycleSnapshot

        return LifecycleSnapshot(
            auth_status=self._status,
            cp_reachable=True,
            last_tickle_at=None,
            last_reauth_at=None,
            last_sso_validate_at=None,
            last_login_at=None,
            session_age_s=None,
            consecutive_reauth_failures=0,
            accounts_initialized=False,
            session_id=None,
            iserver_bridge_ok=None,
            last_bridge_probe_at=None,
            consecutive_bridge_failures=0,
        )

    def update_auto_login(self, **kwargs: Any) -> None:
        self.auto_login_calls.append(kwargs)


class _RecordingRunner:
    def __init__(self, result: AutoLoginResult) -> None:
        self._result = result
        self.calls = 0

    async def run(self) -> AutoLoginResult:
        self.calls += 1
        return self._result


class _FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
def throttle(clock: _FakeClock) -> AutoLoginThrottle:
    return AutoLoginThrottle(clock=clock)


@pytest.fixture
def lifecycle() -> _StaticLifecycle:
    return _StaticLifecycle(status=AuthStatus.AUTH_LOST)


@pytest.fixture
def runner_success() -> _RecordingRunner:
    return _RecordingRunner(AutoLoginResult(exit_code=0, duration_s=1.0))


# ---- Pre-Conditions ----


async def test_skipped_when_disabled(
    lifecycle: _StaticLifecycle,
    throttle: AutoLoginThrottle,
    runner_success: _RecordingRunner,
    clock: _FakeClock,
) -> None:
    trigger = AutoLoginTrigger(
        lifecycle=lifecycle,
        throttle=throttle,
        runner=runner_success,
        enabled=False,
        stack_kind="paper",
        clock=clock,
    )
    outcome = await trigger.maybe_trigger()
    assert outcome.skipped is True
    assert outcome.reason == "disabled"
    assert runner_success.calls == 0


async def test_skipped_when_stack_kind_live(
    lifecycle: _StaticLifecycle,
    throttle: AutoLoginThrottle,
    runner_success: _RecordingRunner,
    clock: _FakeClock,
) -> None:
    """Hard-Guard im Trigger: live-Stack darf NIE Auto-Login starten."""
    trigger = AutoLoginTrigger(
        lifecycle=lifecycle,
        throttle=throttle,
        runner=runner_success,
        enabled=True,
        stack_kind="live",
        clock=clock,
    )
    outcome = await trigger.maybe_trigger()
    assert outcome.skipped is True
    assert outcome.reason == "stack_kind_live"
    assert runner_success.calls == 0


async def test_skipped_when_auth_ok(
    lifecycle: _StaticLifecycle,
    throttle: AutoLoginThrottle,
    runner_success: _RecordingRunner,
    clock: _FakeClock,
) -> None:
    lifecycle.set_status(AuthStatus.OK)
    trigger = AutoLoginTrigger(
        lifecycle=lifecycle,
        throttle=throttle,
        runner=runner_success,
        enabled=True,
        stack_kind="paper",
        clock=clock,
    )
    outcome = await trigger.maybe_trigger()
    assert outcome.skipped is True
    assert outcome.reason == "auth_status_ok"


async def test_skipped_when_reauth_pending(
    lifecycle: _StaticLifecycle,
    throttle: AutoLoginThrottle,
    runner_success: _RecordingRunner,
    clock: _FakeClock,
) -> None:
    """REAUTH_PENDING bedeutet Lifecycle versucht selbst zu heilen —
    Auto-Login soll dem Pfad nicht ins Werk pfuschen."""
    lifecycle.set_status(AuthStatus.REAUTH_PENDING)
    trigger = AutoLoginTrigger(
        lifecycle=lifecycle,
        throttle=throttle,
        runner=runner_success,
        enabled=True,
        stack_kind="paper",
        clock=clock,
    )
    outcome = await trigger.maybe_trigger()
    assert outcome.skipped is True
    assert outcome.reason == "auth_status_reauth_pending"


@pytest.mark.parametrize(
    "status", [AuthStatus.AUTH_LOST, AuthStatus.CP_DOWN]
)
async def test_runs_on_session_loss(
    lifecycle: _StaticLifecycle,
    throttle: AutoLoginThrottle,
    runner_success: _RecordingRunner,
    clock: _FakeClock,
    status: AuthStatus,
) -> None:
    lifecycle.set_status(status)
    trigger = AutoLoginTrigger(
        lifecycle=lifecycle,
        throttle=throttle,
        runner=runner_success,
        enabled=True,
        stack_kind="paper",
        clock=clock,
    )
    outcome = await trigger.maybe_trigger()
    assert outcome.skipped is False
    assert runner_success.calls == 1


async def test_skipped_when_throttle_blocks(
    lifecycle: _StaticLifecycle,
    runner_success: _RecordingRunner,
    clock: _FakeClock,
) -> None:
    throttle = AutoLoginThrottle(clock=clock)
    # Erster Versuch verbraucht den Slot.
    throttle.attempt()
    throttle.record_failure()
    trigger = AutoLoginTrigger(
        lifecycle=lifecycle,
        throttle=throttle,
        runner=runner_success,
        enabled=True,
        stack_kind="paper",
        clock=clock,
    )
    outcome = await trigger.maybe_trigger()
    assert outcome.skipped is True
    assert outcome.reason == "cooldown_5min"
    assert runner_success.calls == 0


# ---- Erfolg / Fehlschlag ----


async def test_success_updates_lifecycle(
    lifecycle: _StaticLifecycle,
    throttle: AutoLoginThrottle,
    runner_success: _RecordingRunner,
    clock: _FakeClock,
) -> None:
    trigger = AutoLoginTrigger(
        lifecycle=lifecycle,
        throttle=throttle,
        runner=runner_success,
        enabled=True,
        stack_kind="paper",
        clock=clock,
    )
    outcome = await trigger.maybe_trigger()
    assert outcome.skipped is False
    assert outcome.result is not None and outcome.result.exit_code == 0
    # 2 Updates: einer beim Start (attempted_at + throttle_state),
    # einer am Ende (succeeded_at).
    assert len(lifecycle.auto_login_calls) >= 2
    last = lifecycle.auto_login_calls[-1]
    assert last["succeeded_at"] is not None
    assert last["throttle_state"] == "cooldown_5min"


async def test_failure_increments_counter(
    lifecycle: _StaticLifecycle,
    throttle: AutoLoginThrottle,
    clock: _FakeClock,
) -> None:
    runner_fail = _RecordingRunner(
        AutoLoginResult(exit_code=2, duration_s=0.5, error="login refused")
    )
    trigger = AutoLoginTrigger(
        lifecycle=lifecycle,
        throttle=throttle,
        runner=runner_fail,
        enabled=True,
        stack_kind="paper",
        clock=clock,
    )
    outcome = await trigger.maybe_trigger()
    assert outcome.skipped is False
    assert outcome.result.exit_code == 2
    last = lifecycle.auto_login_calls[-1]
    assert last["failure_increment"] == 1
    # succeeded_at darf entweder fehlen (keine erfolgreiche Sitzung)
    # oder None sein — Hauptsache nicht ein Datum.
    assert last.get("succeeded_at") is None


async def test_runner_exception_marks_failure(
    lifecycle: _StaticLifecycle,
    throttle: AutoLoginThrottle,
    clock: _FakeClock,
) -> None:
    """Wenn der Runner selbst werft (Docker-Socket-Fehler etc.),
    Throttle muss Failure gezaehlt bekommen — sonst rennt der naechste
    Trigger sofort wieder los."""

    class _CrashingRunner:
        async def run(self) -> AutoLoginResult:
            raise RuntimeError("docker socket gone")

    trigger = AutoLoginTrigger(
        lifecycle=lifecycle,
        throttle=throttle,
        runner=_CrashingRunner(),
        enabled=True,
        stack_kind="paper",
        clock=clock,
    )
    outcome = await trigger.maybe_trigger()
    assert outcome.skipped is False
    assert outcome.result is not None
    # Exit-Code 9 = "anderer Fehler" (Karten-Spec)
    assert outcome.result.exit_code == 9
    last = lifecycle.auto_login_calls[-1]
    assert last["failure_increment"] == 1


# ---- 2FA-Erkennung (Exit-4) ----


async def test_2fa_detected_disables_future_runs(
    lifecycle: _StaticLifecycle,
    throttle: AutoLoginThrottle,
    clock: _FakeClock,
) -> None:
    runner_2fa = _RecordingRunner(
        AutoLoginResult(exit_code=4, duration_s=2.0, error="2FA required")
    )
    trigger = AutoLoginTrigger(
        lifecycle=lifecycle,
        throttle=throttle,
        runner=runner_2fa,
        enabled=True,
        stack_kind="paper",
        clock=clock,
    )
    outcome = await trigger.maybe_trigger()
    assert outcome.skipped is False
    assert outcome.result.exit_code == 4
    last = lifecycle.auto_login_calls[-1]
    # Nach 2FA-Detection wird der throttle_state auf einen
    # menschlichen-Eingriff-Marker gesetzt — der naechste maybe_trigger
    # darf diesen Marker als "skip" werten.
    assert last["throttle_state"] == "2fa_required_manual_intervention"

    # Naechster Trigger-Versuch muss skipen, ohne Runner aufzurufen.
    outcome2 = await trigger.maybe_trigger()
    assert outcome2.skipped is True
    assert outcome2.reason == "2fa_required_manual_intervention"
    assert runner_2fa.calls == 1
