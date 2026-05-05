"""Throttle-Logik fuer den Paper-Stack-Auto-Login.

Schuetzt vor IBKR-Lockout, indem sie konservative Limits setzt:

- max 1 Versuch pro 5 Minuten (``min_interval`` nach Erfolg).
- max 3 Versuche pro Stunde.
- max 5 Versuche pro Tag.
- exponentieller Backoff nach Fehlschlaegen: 5/15/45 min.

IBKR publiziert keine offiziellen Lockout-Schwellen; Forum-Berichte
nennen ~3-5 fehlgeschlagene Logins fuer einen 15-min-Lockout und ~5-10
fuer eine Account-Sperre. Wir bleiben mit max 5/Tag deutlich unter
diesen Werten und vermeiden bot-typisches Hammering.

Die Klasse ist clock-injectable, damit Tests ohne ``time.sleep``
auskommen.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Deque


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AutoLoginThrottleConfig:
    """Konservative Defaults nach Karten-Spec. Production-Code laesst
    die Defaults stehen; Tests koennen die Werte schrumpfen, um echte
    Wartezeiten zu vermeiden."""

    min_interval: timedelta = timedelta(minutes=5)
    max_per_hour: int = 3
    max_per_day: int = 5
    backoff_levels: tuple[timedelta, ...] = (
        timedelta(minutes=5),
        timedelta(minutes=15),
        timedelta(minutes=45),
    )


@dataclass(frozen=True)
class ThrottleDecision:
    """Antwort von ``AutoLoginThrottle.attempt``."""

    allowed: bool
    reason: str
    retry_at: datetime | None = None


@dataclass
class _Attempt:
    at: datetime
    succeeded: bool | None  # None = noch nicht entschieden


class AutoLoginThrottle:
    """Zaehlt Versuche und entscheidet ob der naechste Auto-Login darf.

    Lebenszyklus eines Versuchs::

        decision = throttle.attempt()
        if not decision.allowed:
            return  # blockt, sieht man im Health-Endpoint
        try:
            run_sidecar()
        except Exception:
            throttle.record_failure()
            raise
        throttle.record_success()
    """

    def __init__(
        self,
        config: AutoLoginThrottleConfig | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config or AutoLoginThrottleConfig()
        self._clock = clock or _utcnow
        self._attempts: Deque[_Attempt] = deque()
        self._consecutive_failures = 0
        self._pending_attempt_index: int | None = None

    # ---- oeffentliche API ----

    def attempt(self) -> ThrottleDecision:
        """Versucht, einen Auto-Login-Slot zu reservieren.

        Bei ``allowed=True`` wird intern ein offener Versuch gehalten,
        bis ``record_success`` oder ``record_failure`` ihn schliesst.
        """
        now = self._clock()
        self._purge(now)

        # Tageslimit zaehlt ALLE Versuche der letzten 24h, egal ob
        # erfolgreich oder fail.
        if len(self._attempts) >= self._config.max_per_day:
            oldest = self._attempts[0].at
            return ThrottleDecision(
                allowed=False,
                reason="daily_limit_reached",
                retry_at=oldest + timedelta(days=1),
            )

        # Stundenlimit
        attempts_1h = [a for a in self._attempts if a.at > now - timedelta(hours=1)]
        if len(attempts_1h) >= self._config.max_per_hour:
            return ThrottleDecision(
                allowed=False,
                reason="hourly_limit_reached",
                retry_at=attempts_1h[0].at + timedelta(hours=1),
            )

        # Cooldown nach letztem Versuch
        cooldown = self._current_cooldown()
        if self._attempts and cooldown is not None:
            last = self._attempts[-1].at
            unblock_at = last + cooldown
            if now < unblock_at:
                return ThrottleDecision(
                    allowed=False,
                    reason=self._cooldown_reason(cooldown),
                    retry_at=unblock_at,
                )

        # Erlaubt.
        self._attempts.append(_Attempt(at=now, succeeded=None))
        self._pending_attempt_index = len(self._attempts) - 1
        return ThrottleDecision(allowed=True, reason="ready")

    def record_success(self) -> None:
        """Markiert den letzten offenen Versuch als erfolgreich.

        Setzt den Failure-Streak zurueck. Idempotent gegen doppelte
        Aufrufe, no-op wenn kein offener Versuch existiert (defensive
        Pruefung gegen falsche Caller-Reihenfolge).
        """
        if not self._close_pending(succeeded=True):
            return
        self._consecutive_failures = 0

    def record_failure(self) -> None:
        """Markiert den letzten offenen Versuch als fehlgeschlagen.

        Erhoeht den Failure-Streak — der naechste Cooldown wird laenger.
        """
        if not self._close_pending(succeeded=False):
            return
        self._consecutive_failures += 1

    def state(self) -> str:
        """Aktueller Zustand fuer Health-Endpoint und Logs.

        Mapping:

        - ``ready``: keine Limits aktiv, naechster Versuch wuerde laufen.
        - ``cooldown_5min`` / ``cooldown_15min`` / ``cooldown_45min``:
          Backoff-Cooldown nach Fehlschlag laeuft.
        - ``hourly_limit_reached`` / ``daily_limit_reached``: harter Stop.
        """
        now = self._clock()
        self._purge(now)

        if len(self._attempts) >= self._config.max_per_day:
            return "daily_limit_reached"
        attempts_1h = [a for a in self._attempts if a.at > now - timedelta(hours=1)]
        if len(attempts_1h) >= self._config.max_per_hour:
            return "hourly_limit_reached"

        cooldown = self._current_cooldown()
        if self._attempts and cooldown is not None:
            last = self._attempts[-1].at
            if now < last + cooldown:
                return self._cooldown_reason(cooldown)
        return "ready"

    # ---- Interna ----

    def _purge(self, now: datetime) -> None:
        cutoff = now - timedelta(days=1)
        while self._attempts and self._attempts[0].at < cutoff:
            removed = self._attempts.popleft()
            if self._pending_attempt_index is not None:
                self._pending_attempt_index -= 1
                if self._pending_attempt_index < 0:
                    self._pending_attempt_index = None
            del removed

    def _close_pending(self, *, succeeded: bool) -> bool:
        if self._pending_attempt_index is None:
            return False
        if not (0 <= self._pending_attempt_index < len(self._attempts)):
            self._pending_attempt_index = None
            return False
        attempt = self._attempts[self._pending_attempt_index]
        attempt.succeeded = succeeded
        self._pending_attempt_index = None
        return True

    def _current_cooldown(self) -> timedelta | None:
        """Cooldown, der seit dem letzten Versuch eingehalten werden muss."""
        if not self._attempts:
            return None
        last = self._attempts[-1]
        if last.succeeded is None:
            # Noch offener Versuch — kein zweiter Versuch parallel.
            return self._config.min_interval
        if last.succeeded:
            return self._config.min_interval
        # Fehlschlag: exponentieller Backoff nach Streak-Index.
        levels = self._config.backoff_levels
        if not levels:
            return self._config.min_interval
        idx = min(max(self._consecutive_failures - 1, 0), len(levels) - 1)
        return levels[idx]

    def _cooldown_reason(self, cooldown: timedelta) -> str:
        minutes = int(cooldown.total_seconds() // 60)
        if minutes <= 0:
            seconds = int(cooldown.total_seconds())
            return f"cooldown_{seconds}s"
        return f"cooldown_{minutes}min"


__all__ = [
    "AutoLoginThrottle",
    "AutoLoginThrottleConfig",
    "ThrottleDecision",
]
