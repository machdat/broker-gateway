"""Tests fuer AutoLoginThrottle.

Konservative Limits laut Karten-Spec:
- max 1 Versuch pro 5 Minuten (min_interval)
- max 3 Versuche pro Stunde
- max 5 Versuche pro Tag
- Backoff nach Fehlschlag: 5/15/45min (exponentiell)
- Nach Tages-Limit: Stop bis Tagesgrenze
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from broker_gateway.cp.auto_login_throttle import (
    AutoLoginThrottle,
    AutoLoginThrottleConfig,
    ThrottleDecision,
)


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)


@pytest.fixture
def clock() -> _FakeClock:
    return _FakeClock(datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def throttle(clock: _FakeClock) -> AutoLoginThrottle:
    return AutoLoginThrottle(clock=clock)


# ---- Erstaufruf ----


def test_first_attempt_allowed(throttle: AutoLoginThrottle) -> None:
    decision = throttle.attempt()
    assert decision.allowed is True
    assert decision.reason == "ready"


def test_state_ready_initially(throttle: AutoLoginThrottle) -> None:
    assert throttle.state() == "ready"


# ---- min_interval (5min nach Erfolg) ----


def test_second_attempt_blocked_within_5min_after_success(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    throttle.attempt()
    throttle.record_success()
    clock.advance(minutes=4, seconds=59)
    decision = throttle.attempt()
    assert decision.allowed is False
    assert decision.reason.startswith("cooldown")


def test_second_attempt_allowed_after_5min_post_success(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    throttle.attempt()
    throttle.record_success()
    clock.advance(minutes=5, seconds=1)
    decision = throttle.attempt()
    assert decision.allowed is True


# ---- Backoff nach Fehlschlag ----


def test_first_failure_5min_cooldown(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    throttle.attempt()
    throttle.record_failure()
    clock.advance(minutes=4, seconds=30)
    decision = throttle.attempt()
    assert decision.allowed is False
    assert decision.reason == "cooldown_5min"


def test_first_failure_unblocks_after_5min(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    throttle.attempt()
    throttle.record_failure()
    clock.advance(minutes=5, seconds=1)
    decision = throttle.attempt()
    assert decision.allowed is True


def test_second_failure_15min_cooldown(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    # 1. Versuch -> fail
    throttle.attempt()
    throttle.record_failure()
    clock.advance(minutes=5, seconds=1)
    # 2. Versuch -> fail
    throttle.attempt()
    throttle.record_failure()
    clock.advance(minutes=14, seconds=59)
    decision = throttle.attempt()
    assert decision.allowed is False
    assert decision.reason == "cooldown_15min"


def test_third_failure_45min_cooldown(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    for cooldown in (5, 15):
        throttle.attempt()
        throttle.record_failure()
        clock.advance(minutes=cooldown, seconds=1)
    # 3. Versuch -> fail (45min Cooldown)
    throttle.attempt()
    throttle.record_failure()
    clock.advance(minutes=44, seconds=59)
    decision = throttle.attempt()
    assert decision.allowed is False
    assert decision.reason == "cooldown_45min"


def test_success_resets_failure_streak(clock: _FakeClock) -> None:
    """Nach einem record_success darf der naechste Fehler-Cooldown
    wieder bei 5min anfangen, nicht bei 15min."""
    # max_per_hour hochsetzen, damit der Test den Streak-Reset isoliert
    # untersuchen kann ohne in das Hourly-Limit zu laufen.
    cfg = AutoLoginThrottleConfig(max_per_hour=10, max_per_day=20)
    throttle = AutoLoginThrottle(config=cfg, clock=clock)
    throttle.attempt()
    throttle.record_failure()
    clock.advance(minutes=5, seconds=1)
    throttle.attempt()
    throttle.record_success()
    clock.advance(minutes=5, seconds=1)
    throttle.attempt()
    throttle.record_failure()
    clock.advance(minutes=4, seconds=59)
    decision = throttle.attempt()
    assert decision.allowed is False
    assert decision.reason == "cooldown_5min"


# ---- Hourly-Limit (max 3 pro Stunde) ----


def test_hourly_limit_blocks_4th_attempt(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    # 3 Versuche in einer Stunde, je 6min Abstand (umgeht min_interval=5min).
    for _ in range(3):
        decision = throttle.attempt()
        assert decision.allowed is True
        throttle.record_success()
        clock.advance(minutes=6)
    decision = throttle.attempt()
    assert decision.allowed is False
    assert decision.reason == "hourly_limit_reached"


def test_hourly_limit_unblocks_after_window(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    for _ in range(3):
        throttle.attempt()
        throttle.record_success()
        clock.advance(minutes=6)
    # Jetzt 1h nach erstem Versuch (3 * 6min = 18min) -> 60-18 = 42min
    # warten, damit der erste aus dem 1h-Fenster faellt.
    clock.advance(minutes=43)
    decision = throttle.attempt()
    assert decision.allowed is True


# ---- Daily-Limit (max 5 pro Tag) ----


def test_daily_limit_blocks_6th_attempt(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    # 5 Versuche, dazwischen je 30min Abstand (umgeht hourly via 3-pro-h
    # und min_interval).
    for _ in range(5):
        decision = throttle.attempt()
        assert decision.allowed is True
        throttle.record_success()
        # 30min ist groesser als min_interval und groesser als 1h/3 = 20min
        # Bei 30min: nach 3 Versuchen sind 90min vergangen, 1h-Fenster
        # enthaelt nur den letzten -> wir bleiben unter 3/h.
        clock.advance(minutes=30)
    decision = throttle.attempt()
    assert decision.allowed is False
    assert decision.reason == "daily_limit_reached"


def test_daily_limit_unblocks_after_24h(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    for _ in range(5):
        throttle.attempt()
        throttle.record_success()
        clock.advance(minutes=30)
    # Nach 5 Versuchen + 4*30min Wartezeit sind 2h vergangen seit dem
    # ersten Versuch. 24h - 2h = 22h warten, damit der erste aus dem
    # Tag-Fenster faellt.
    clock.advance(hours=22, minutes=1)
    decision = throttle.attempt()
    assert decision.allowed is True


# ---- state() spiegelt das aktuelle Verhalten ----


def test_state_after_failure(throttle: AutoLoginThrottle, clock: _FakeClock) -> None:
    throttle.attempt()
    throttle.record_failure()
    assert throttle.state() == "cooldown_5min"
    clock.advance(minutes=5, seconds=1)
    assert throttle.state() == "ready"


def test_state_daily_limit(throttle: AutoLoginThrottle, clock: _FakeClock) -> None:
    for _ in range(5):
        throttle.attempt()
        throttle.record_success()
        clock.advance(minutes=30)
    assert throttle.state() == "daily_limit_reached"


def test_state_hourly_limit(throttle: AutoLoginThrottle, clock: _FakeClock) -> None:
    for _ in range(3):
        throttle.attempt()
        throttle.record_success()
        clock.advance(minutes=6)
    assert throttle.state() == "hourly_limit_reached"


# ---- record_*-Aufrufe nur nach attempt() (Defensive) ----


def test_record_without_attempt_is_noop(throttle: AutoLoginThrottle) -> None:
    # Keine Exception. Idempotent.
    throttle.record_success()
    throttle.record_failure()
    decision = throttle.attempt()
    assert decision.allowed is True


# ---- Decision enthaelt retry_at fuer Caller-Logging ----


def test_decision_includes_retry_at_when_blocked(
    throttle: AutoLoginThrottle, clock: _FakeClock
) -> None:
    throttle.attempt()
    throttle.record_failure()
    decision = throttle.attempt()
    assert decision.allowed is False
    assert decision.retry_at is not None
    assert decision.retry_at > clock()


# ---- Custom config moeglich ----


def test_custom_config(clock: _FakeClock) -> None:
    cfg = AutoLoginThrottleConfig(
        min_interval=timedelta(seconds=10),
        max_per_hour=2,
        max_per_day=3,
        backoff_levels=(timedelta(seconds=10), timedelta(seconds=20)),
    )
    t = AutoLoginThrottle(config=cfg, clock=clock)
    t.attempt()
    t.record_success()
    clock.advance(seconds=11)
    t.attempt()
    t.record_success()
    decision = t.attempt()
    assert decision.allowed is False
    assert decision.reason == "hourly_limit_reached"
