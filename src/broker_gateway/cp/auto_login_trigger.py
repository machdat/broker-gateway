"""Auto-Login-Trigger fuer den Paper-Stack (Karte ece90a8e).

Die Klasse entscheidet, ob ein Auto-Login angestossen wird, und ruft
dafuer einen injizierten ``AutoLoginRunner`` auf. Der Runner ist in
Phase A ein Mock (Tests); in Phase B implementiert er den echten
Sidecar-Aufruf via Docker-Socket + Playwright/Chromium.

**Hard-Guard-Schichten** (defensiv mehrfach geprueft):

1. ``main.py`` ruft ``validate_runtime_config`` beim Startup —
   ``BG_STACK_KIND=live`` + ``BG_PAPER_AUTO_LOGIN=1`` ist dort schon
   ein Startup-Fail, der Trigger laeuft also gar nicht erst.
2. Trigger-Konstruktor erhaelt ``stack_kind`` als Pflicht-Argument;
   Live-Stack wird in ``maybe_trigger`` mit ``stack_kind_live``
   geskippt — selbst wenn aus Versehen ein Trigger im Live-Stack
   instantiiert wuerde.
3. (Phase B) Sidecar-Skript pruefte selbst, dass die Ziel-URL
   ``paper-cpgateway`` enthaelt (Exit-5).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol


logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AutoLoginResult:
    """Ergebnis eines Sidecar-Aufrufs.

    Exit-Codes nach Karten-Spec:

    - ``0``: Erfolg
    - ``1``: Form nicht gefunden / Selector-Drift
    - ``2``: Login abgelehnt (Credentials oder Captcha)
    - ``3``: paper-cpgateway nicht erreichbar
    - ``4``: 2FA-Pflicht erkannt — IBKR hat Policy geaendert
    - ``5``: Hard-Guard-Verletzung im Sidecar
    - ``9``: anderer Fehler (z.B. Runner-Crash)
    """

    exit_code: int
    duration_s: float
    error: str | None = None


class AutoLoginRunner(Protocol):
    """Abstrakter Sidecar-Aufruf.

    Phase A: Tests injizieren einen Mock. Phase B liefert die echte
    docker-SDK-Implementation.
    """

    async def run(self) -> AutoLoginResult:
        ...


@dataclass(frozen=True)
class TriggerOutcome:
    """Antwort von ``AutoLoginTrigger.maybe_trigger``."""

    skipped: bool
    reason: str
    result: AutoLoginResult | None = None


class _AutoLoginLifecycleHook(Protocol):
    """Schmaler Slice von ``AuthLifecycle``, den der Trigger braucht.

    Bewusst kein direkter ``AuthLifecycle``-Import als Typ — das haelt
    den Trigger testbar ohne Tickle-Loop und vermeidet Zirkular-
    Importe.
    """

    def snapshot(self): ...
    def update_auto_login(
        self,
        *,
        attempted_at: datetime | None = None,
        succeeded_at: datetime | None = None,
        failure_increment: int = 0,
        throttle_state: str | None = None,
    ) -> None: ...


_TRIGGER_AUTH_STATES = {"auth_lost", "cp_down"}


class AutoLoginTrigger:
    """Pre-Conditions + Throttle + Runner-Aufruf.

    Hard-Guards (mehrfach geprueft, siehe Modul-Docstring) und
    Zustands-Machine sind hier zentralisiert — der Runner kennt nur
    seinen eigenen Sidecar-Pfad.
    """

    def __init__(
        self,
        *,
        lifecycle: _AutoLoginLifecycleHook,
        throttle,
        runner: AutoLoginRunner,
        enabled: bool,
        stack_kind: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._lifecycle = lifecycle
        self._throttle = throttle
        self._runner = runner
        self._enabled = enabled
        self._stack_kind = stack_kind
        self._clock = clock or _utcnow
        # Sticky-Stop nach 2FA-Detection: einmal Exit-4 gesehen, fuehrt
        # jeder weitere maybe_trigger-Aufruf zum skip — auch nach
        # Throttle-Reset. Manueller Eingriff ist gefordert.
        self._manual_intervention_required = False

    async def maybe_trigger(self) -> TriggerOutcome:
        """Gate: pruefe Pre-Conditions und triggere ggf. den Runner."""
        if self._manual_intervention_required:
            return TriggerOutcome(
                skipped=True, reason="2fa_required_manual_intervention"
            )

        if not self._enabled:
            return TriggerOutcome(skipped=True, reason="disabled")

        if self._stack_kind != "paper":
            return TriggerOutcome(skipped=True, reason=f"stack_kind_{self._stack_kind}")

        snap = self._lifecycle.snapshot()
        status_value = getattr(snap.auth_status, "value", str(snap.auth_status))
        if status_value not in _TRIGGER_AUTH_STATES:
            # Auch REAUTH_PENDING wird hier geskippt — der Lifecycle
            # versucht selbst zu heilen, der Sidecar wuerde nur
            # konkurrierend rumlaufen.
            return TriggerOutcome(skipped=True, reason=f"auth_status_{status_value}")

        decision = self._throttle.attempt()
        if not decision.allowed:
            return TriggerOutcome(skipped=True, reason=decision.reason)

        # Versuch starten.
        attempt_started_at = self._clock()
        self._lifecycle.update_auto_login(
            attempted_at=attempt_started_at,
            throttle_state="running",
        )
        logger.info("auto-login: triggering sidecar (stack=%s)", self._stack_kind)

        try:
            result = await self._runner.run()
        except Exception as exc:  # noqa: BLE001 - top-level guard
            logger.exception("auto-login: runner raised %s", type(exc).__name__)
            self._throttle.record_failure()
            self._lifecycle.update_auto_login(
                failure_increment=1,
                throttle_state=self._throttle.state(),
            )
            return TriggerOutcome(
                skipped=False,
                reason="runner_crashed",
                result=AutoLoginResult(
                    exit_code=9, duration_s=0.0, error=str(exc) or type(exc).__name__
                ),
            )

        # 2FA-Detection ist sticky.
        if result.exit_code == 4:
            self._manual_intervention_required = True
            self._throttle.record_failure()
            self._lifecycle.update_auto_login(
                failure_increment=1,
                throttle_state="2fa_required_manual_intervention",
            )
            logger.warning(
                "auto-login: 2FA required (exit=4) — manual intervention until restart"
            )
            return TriggerOutcome(skipped=False, reason="2fa_required", result=result)

        if result.exit_code == 0:
            self._throttle.record_success()
            self._lifecycle.update_auto_login(
                succeeded_at=self._clock(),
                throttle_state=self._throttle.state(),
            )
            logger.info("auto-login: sidecar succeeded (duration=%.1fs)", result.duration_s)
            return TriggerOutcome(skipped=False, reason="success", result=result)

        # Anderer Fehler (Exit 1/2/3/5/9).
        self._throttle.record_failure()
        self._lifecycle.update_auto_login(
            failure_increment=1,
            throttle_state=self._throttle.state(),
        )
        logger.warning(
            "auto-login: sidecar failed (exit=%s, error=%s)", result.exit_code, result.error
        )
        return TriggerOutcome(
            skipped=False, reason=f"exit_{result.exit_code}", result=result
        )


__all__ = [
    "AutoLoginResult",
    "AutoLoginRunner",
    "AutoLoginTrigger",
    "TriggerOutcome",
]
