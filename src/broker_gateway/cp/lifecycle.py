"""Auth-Lifecycle: Hintergrund-Tickle + Reauth-State-Machine.

Single Source of Truth für den aktuellen Session-Zustand. Status-Felder
und Timestamps leben ausschließlich in `AuthLifecycle.snapshot()` -
nirgends im Code dürfen Kopien dieser Werte gehalten werden.
"""
from __future__ import annotations

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException, status

from broker_gateway.cp.client import CPGatewayClient


logger = logging.getLogger(__name__)


class AuthStatus(str, enum.Enum):
    OK = "ok"
    REAUTH_PENDING = "reauth_pending"
    AUTH_LOST = "auth_lost"
    CP_DOWN = "cp_down"


_DEFAULT_TICKLE_INTERVAL_S = 60.0
_DEFAULT_REAUTH_MAX_RETRIES = 3
_DEFAULT_REAUTH_BACKOFF_S = 2.0
_RETRY_AFTER_S = 30
# iserver-Backend-Bridge-Probe (Karte 81e3c818): zusaetzlicher Health-Pfad,
# der ueber GET /iserver/auth/status das Trio (authenticated/established/
# connected) prueft. Ein gefallener iserver-Bridge laesst SSO-Tickle und
# sso/validate weiterhin gruen, bricht aber jeden iserver-Call mit
# "Bad Request: no bridge". Recovery: einmaliger force-reauth + 15s warmup.
_DEFAULT_BRIDGE_PROBE_INTERVAL_S = 120.0
_DEFAULT_BRIDGE_REAUTH_WARMUP_S = 15.0
_DEFAULT_BRIDGE_MAX_FAILURES = 3


def _interval_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ENV %s=%r ist keine gültige Zahl, nutze Default %s", name, raw, default)
        return default


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LifecycleSnapshot:
    """Read-only Sicht auf den aktuellen Lifecycle-State."""

    auth_status: AuthStatus
    cp_reachable: bool
    last_tickle_at: datetime | None
    last_reauth_at: datetime | None
    last_sso_validate_at: datetime | None
    last_login_at: datetime | None
    session_age_s: float | None
    consecutive_reauth_failures: int
    accounts_initialized: bool
    session_id: str | None
    # iserver-Bridge-Probe-Felder (Karte 81e3c818):
    # `iserver_bridge_ok=None` heisst "noch nie probet", `True/False`
    # spiegelt das letzte GET /iserver/auth/status-Resultat.
    iserver_bridge_ok: bool | None
    last_bridge_probe_at: datetime | None
    consecutive_bridge_failures: int
    # Auto-Login-Felder (Karte ece90a8e, Phase A):
    # `last_auto_login_attempt_at=None` heisst "Auto-Login wurde noch
    # nie versucht". `auto_login_throttle_state="ready"` ist der
    # Default — siehe `cp.auto_login_throttle.AutoLoginThrottle.state()`
    # fuer das vollstaendige Mapping.
    last_auto_login_attempt_at: datetime | None = None
    last_auto_login_success_at: datetime | None = None
    auto_login_failures_total: int = 0
    auto_login_throttle_state: str = "ready"


class AuthLifecycle:
    """Hält genau eine IBKR-Session offen.

    Architektur:
    - Ein einziger asyncio-Task ruft zyklisch tickle().
    - Liefert tickle() einen Auth-Verlust, wechselt der Status auf
      REAUTH_PENDING. Es folgen bis zu `reauth_max_retries` reauthenticate-
      Versuche mit exponential backoff, dann AUTH_LOST.
    - HTTP-Fehler (Connection refused, Timeout) => CP_DOWN.
    - Sobald ein Tickle wieder authenticated=true liefert, springt der
      Status zurück auf OK und session_started_at wird (re-)gesetzt.
    """

    def __init__(
        self,
        client: CPGatewayClient,
        *,
        tickle_interval_s: float | None = None,
        reauth_max_retries: int = _DEFAULT_REAUTH_MAX_RETRIES,
        reauth_backoff_s: float = _DEFAULT_REAUTH_BACKOFF_S,
        bridge_probe_interval_s: float | None = None,
        bridge_reauth_warmup_s: float = _DEFAULT_BRIDGE_REAUTH_WARMUP_S,
        bridge_max_failures: int = _DEFAULT_BRIDGE_MAX_FAILURES,
    ) -> None:
        self._client = client
        self.tickle_interval_s = (
            tickle_interval_s
            if tickle_interval_s is not None
            else _interval_from_env("BG_CP_TICKLE_INTERVAL_S", _DEFAULT_TICKLE_INTERVAL_S)
        )
        self.reauth_max_retries = reauth_max_retries
        self.reauth_backoff_s = reauth_backoff_s
        self.bridge_probe_interval_s = (
            bridge_probe_interval_s
            if bridge_probe_interval_s is not None
            else _interval_from_env(
                "BG_CP_BRIDGE_PROBE_INTERVAL_S", _DEFAULT_BRIDGE_PROBE_INTERVAL_S
            )
        )
        self.bridge_reauth_warmup_s = bridge_reauth_warmup_s
        self.bridge_max_failures = bridge_max_failures

        self._status = AuthStatus.OK
        self._cp_reachable = True
        self._last_tickle_at: datetime | None = None
        self._last_reauth_at: datetime | None = None
        self._last_sso_validate_at: datetime | None = None
        self._last_login_at: datetime | None = None
        self._session_started_at: float | None = None
        self._reauth_failures = 0
        self._accounts_initialized = False
        self._session_id: str | None = None
        self._iserver_bridge_ok: bool | None = None
        self._last_bridge_probe_at: datetime | None = None
        self._last_bridge_probe_monotonic: float | None = None
        self._consecutive_bridge_failures = 0
        self._last_auto_login_attempt_at: datetime | None = None
        self._last_auto_login_success_at: datetime | None = None
        self._auto_login_failures_total = 0
        self._auto_login_throttle_state = "ready"

        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    # ---- öffentliche Status-Sicht ----

    @property
    def status(self) -> AuthStatus:
        return self._status

    @property
    def session_id(self) -> str | None:
        return self._session_id

    def snapshot(self) -> LifecycleSnapshot:
        age: float | None
        if self._session_started_at is None:
            age = None
        else:
            age = max(0.0, time.monotonic() - self._session_started_at)
        return LifecycleSnapshot(
            auth_status=self._status,
            cp_reachable=self._cp_reachable,
            last_tickle_at=self._last_tickle_at,
            last_reauth_at=self._last_reauth_at,
            last_sso_validate_at=self._last_sso_validate_at,
            last_login_at=self._last_login_at,
            session_age_s=age,
            consecutive_reauth_failures=self._reauth_failures,
            accounts_initialized=self._accounts_initialized,
            session_id=self._session_id,
            iserver_bridge_ok=self._iserver_bridge_ok,
            last_bridge_probe_at=self._last_bridge_probe_at,
            consecutive_bridge_failures=self._consecutive_bridge_failures,
            last_auto_login_attempt_at=self._last_auto_login_attempt_at,
            last_auto_login_success_at=self._last_auto_login_success_at,
            auto_login_failures_total=self._auto_login_failures_total,
            auto_login_throttle_state=self._auto_login_throttle_state,
        )

    def update_auto_login(
        self,
        *,
        attempted_at: datetime | None = None,
        succeeded_at: datetime | None = None,
        failure_increment: int = 0,
        throttle_state: str | None = None,
    ) -> None:
        """Update der Auto-Login-Status-Felder im Lifecycle-Snapshot.

        Wird vom ``AutoLoginTrigger`` aufgerufen. Bewusst partial:
        nur die uebergebenen Argumente werden gesetzt, der Rest bleibt
        unveraendert. ``failure_increment`` ist additiv — der Trigger
        muss nicht den aktuellen Stand mitlesen.
        """
        if attempted_at is not None:
            self._last_auto_login_attempt_at = attempted_at
        if succeeded_at is not None:
            self._last_auto_login_success_at = succeeded_at
        if failure_increment:
            self._auto_login_failures_total += failure_increment
        if throttle_state is not None:
            self._auto_login_throttle_state = throttle_state

    # ---- Lifecycle-Steuerung ----

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        # Erster Tickle synchron, damit der Status beim Startup sinnvoll ist.
        await self.tick_once()
        # Initialer Bridge-Probe nur, wenn die SSO-Session steht; sonst
        # wuerde GET /iserver/auth/status sowieso die volle no-bridge-Antwort
        # liefern und unnoetig den Failure-Counter beruehren.
        if self._status is AuthStatus.OK:
            await self.bridge_probe_once()
        self._task = asyncio.create_task(self._run(), name="cp-tickle-loop")

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        self._stop_event = None

    async def _run(self) -> None:
        assert self._stop_event is not None
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.tickle_interval_s)
                except asyncio.TimeoutError:
                    pass
                if self._stop_event.is_set():
                    break
                await self.tick_once()
                # Bridge-Probe laeuft mit eigenem Intervall (Default 120s),
                # also typischerweise jeden zweiten Tickle. Nur wenn die
                # SSO-Session steht; bei AUTH_LOST/CP_DOWN bringt der Probe
                # nichts und der Tickle-Loop kuemmert sich um Recovery.
                if (
                    self._status is AuthStatus.OK
                    and self._should_probe_bridge_now()
                ):
                    await self.bridge_probe_once()
        except asyncio.CancelledError:
            raise

    def _should_probe_bridge_now(self) -> bool:
        if self._last_bridge_probe_monotonic is None:
            return True
        elapsed = time.monotonic() - self._last_bridge_probe_monotonic
        return elapsed >= self.bridge_probe_interval_s

    # ---- ein Tickle-Zyklus ----

    async def tick_once(self) -> AuthStatus:
        # Primaerer Heartbeat: GET /sso/validate (laut IBKR-OpenAPI-Spec
        # der vorgesehene Keep-Alive). Tickle bleibt als sekundaerer
        # CP-Health-Indicator und Backward-Compat-Pfad erhalten.
        sso_authenticated = await self._heartbeat_sso()

        try:
            payload = await self._client.tickle()
        except httpx.HTTPError as exc:
            logger.warning("Tickle-Call fehlgeschlagen: %s", exc)
            # Wenn auch sso/validate nicht authenticated antwortete, ist
            # das CP-Gateway insgesamt nicht erreichbar.
            if not sso_authenticated:
                self._cp_reachable = False
                self._status = AuthStatus.CP_DOWN
                return self._status
            # sso/validate hat geantwortet, also CP erreichbar - aber
            # ohne Tickle koennen wir den authenticated-Wert nicht aus
            # dem Tickle-Body cross-checken. Wir vertrauen sso/validate.
            self._cp_reachable = True
            self._mark_session_ok()
            await self._maybe_init_accounts()
            return self._status

        self._cp_reachable = True
        self._last_tickle_at = _utcnow()
        session_value = payload.get("session")
        if isinstance(session_value, str) and session_value:
            self._session_id = session_value
        if sso_authenticated or _is_authenticated(payload):
            self._mark_session_ok()
            await self._maybe_init_accounts()
            return self._status

        # Auth-Verlust: Reauth-Loop bis Erfolg oder max-retries.
        await self._handle_auth_loss()
        return self._status

    async def _heartbeat_sso(self) -> bool:
        """GET /sso/validate als primaerer Keep-Alive. Returnt True,
        wenn `RESULT=true` zurueckkommt (Session valide)."""
        try:
            payload = await self._client.sso_validate()
        except httpx.HTTPError as exc:
            logger.debug("sso/validate-Call fehlgeschlagen: %s", exc)
            return False
        self._last_sso_validate_at = _utcnow()
        return _is_sso_validated(payload)

    async def _maybe_init_accounts(self) -> None:
        """Beim ersten erfolgreichen Tickle nach Login GET
        /iserver/accounts ausloesen. IBKR erwartet diesen Call vor dem
        ersten Order- oder Portfolio-Aufruf."""
        if self._accounts_initialized:
            return
        try:
            await self._client.iserver_accounts()
        except httpx.HTTPError as exc:
            logger.warning("iserver/accounts-Init fehlgeschlagen: %s", exc)
            return
        self._accounts_initialized = True

    async def _handle_auth_loss(self) -> None:
        self._status = AuthStatus.REAUTH_PENDING
        for attempt in range(1, self.reauth_max_retries + 1):
            self._last_reauth_at = _utcnow()
            try:
                await self._client.reauthenticate()
            except httpx.HTTPError as exc:
                logger.warning("Reauth-Versuch %s fehlgeschlagen: %s", attempt, exc)
                self._reauth_failures = attempt
                await self._sleep(self.reauth_backoff_s * (2 ** (attempt - 1)))
                continue
            await self._sleep(self.reauth_backoff_s)
            try:
                check = await self._client.auth_status()
            except httpx.HTTPError as exc:
                logger.warning("auth_status nach Reauth fehlgeschlagen: %s", exc)
                self._reauth_failures = attempt
                continue
            if _is_authenticated(check):
                self._mark_session_ok()
                return
            self._reauth_failures = attempt
        self._status = AuthStatus.AUTH_LOST

    async def bridge_probe_once(self) -> bool | None:
        """Pruefe via GET /iserver/auth/status, ob der iserver-Bridge zu IBKR
        steht. Im no-bridge-Zustand liefert das Trio
        (authenticated/connected/established) drei false-Werte; SSO-Tickle
        bleibt davon unberuehrt.

        Bei Drift: einmaliger force-reauth + 15s warmup + erneuter Probe.
        Nach `bridge_max_failures` (Default 3) aufeinanderfolgenden Drift-
        Befunden eskaliert der Status pragmatisch auf ``AUTH_LOST`` -
        Business-Endpunkte antworten dann mit 503 + Retry-After.

        Returns:
            ``True`` / ``False`` aus dem letzten Probe-Resultat,
            ``None`` wenn der Probe selbst (Network) fehlschlug und der
            bisherige Bridge-State unveraendert blieb.
        """
        try:
            payload = await self._client.auth_status()
        except httpx.HTTPError as exc:
            logger.debug("bridge_probe: auth_status fehlgeschlagen: %s", exc)
            # Probe konnte nicht laufen; den bisherigen Bridge-State
            # bewusst nicht modifizieren (keine False-Negative).
            return None
        self._record_probe_timestamp()
        if _is_bridge_ok(payload):
            self._iserver_bridge_ok = True
            self._consecutive_bridge_failures = 0
            return True
        # Drift erkannt: SSO-Session steht (sonst waere tick_once schon im
        # Reauth-Loop), aber das iserver-Backend hat die Bruecke verloren.
        # Einmalige Recovery: force-reauth + warmup + recheck.
        self._iserver_bridge_ok = False
        recovered = await self._try_bridge_recover()
        if recovered:
            self._iserver_bridge_ok = True
            self._consecutive_bridge_failures = 0
            return True
        self._consecutive_bridge_failures += 1
        if self._consecutive_bridge_failures >= self.bridge_max_failures:
            # Pragmatische Eskalation: AUTH_LOST mappt direkt auf das
            # bestehende 503-Verhalten (`require_session_ok`). Wir setzen
            # den Status hier ohne nochmaligen Reauth-Loop, weil der erste
            # Reauth-Versuch im _try_bridge_recover gerade fehlgeschlagen ist.
            self._status = AuthStatus.AUTH_LOST
        return False

    async def _try_bridge_recover(self) -> bool:
        """Einmaliger force-reauth + warmup + recheck. Returns True nur,
        wenn der recheck wieder das gesunde Trio sieht."""
        self._last_reauth_at = _utcnow()
        try:
            await self._client.reauthenticate()
        except httpx.HTTPError as exc:
            logger.warning("bridge-recover: reauth fehlgeschlagen: %s", exc)
            return False
        await self._sleep(self.bridge_reauth_warmup_s)
        try:
            recheck = await self._client.auth_status()
        except httpx.HTTPError as exc:
            logger.warning("bridge-recover: recheck-auth_status fehlgeschlagen: %s", exc)
            return False
        self._record_probe_timestamp()
        return _is_bridge_ok(recheck)

    def _record_probe_timestamp(self) -> None:
        self._last_bridge_probe_at = _utcnow()
        self._last_bridge_probe_monotonic = time.monotonic()

    def _mark_session_ok(self) -> None:
        # Wir wechseln in OK, wenn entweder der Status vorher nicht OK
        # war ODER es noch keinen Session-Start gibt - der initial-State
        # `AuthStatus.OK` ist nur ein Default vor dem ersten Tickle und
        # gibt keine echte authentifizierte Session her.
        if self._status is not AuthStatus.OK or self._session_started_at is None:
            self._session_started_at = time.monotonic()
            self._last_login_at = _utcnow()
        self._status = AuthStatus.OK
        self._reauth_failures = 0

    async def reauthenticate(self, *, force: bool = False) -> AuthStatus:
        """Manuell ausgeloester Reauth. Default verhaelt sich wie der
        bisherige Pfad: nur bei REAUTH_PENDING / AUTH_LOST wird der
        Reauth-Loop gestartet. Mit ``force=True`` triggert die Methode
        unconditional ``POST /iserver/reauthenticate`` und prueft den
        Auth-Status danach - das hilft in Edge-Cases wie der in
        ``project_ibkr_session_resume`` dokumentierten
        cold-tunnel-Situation, in der ``auth/status`` faelschlich
        ``authenticated=false`` meldet, der Reauth aber sofort durchgeht.
        """
        if not force:
            if self._status not in (AuthStatus.REAUTH_PENDING, AuthStatus.AUTH_LOST):
                return self._status
            await self._handle_auth_loss()
            return self._status

        self._last_reauth_at = _utcnow()
        try:
            await self._client.reauthenticate()
        except httpx.HTTPError as exc:
            logger.warning("Force-Reauth-Call fehlgeschlagen: %s", exc)
            self._reauth_failures += 1
            return self._status
        await self._sleep(self.reauth_backoff_s)
        try:
            check = await self._client.auth_status()
        except httpx.HTTPError as exc:
            logger.warning("auth_status nach Force-Reauth fehlgeschlagen: %s", exc)
            self._reauth_failures += 1
            return self._status
        if _is_authenticated(check):
            self._mark_session_ok()
        else:
            self._reauth_failures += 1
        return self._status

    async def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        await asyncio.sleep(seconds)


def _is_authenticated(payload: dict[str, object]) -> bool:
    if "authenticated" in payload:
        return bool(payload["authenticated"])
    iserver = payload.get("iserver")
    if isinstance(iserver, dict):
        auth_status = iserver.get("authStatus")
        if isinstance(auth_status, dict) and "authenticated" in auth_status:
            return bool(auth_status["authenticated"])
    return False


def _is_sso_validated(payload: dict[str, object]) -> bool:
    """sso/validate liefert ``{RESULT: bool, ...}``. RESULT=true heisst
    Session valide; alles andere wird als ``not authenticated``
    behandelt."""
    return bool(payload.get("RESULT"))


def _is_bridge_ok(payload: dict[str, object]) -> bool:
    """iserver/auth/status meldet die iserver-Bridge als gesund, wenn das
    Trio ``authenticated/established/connected`` alle drei true sind. Im
    no-bridge-Zustand sind alle drei false, waehrend sso/validate
    weiterhin ``RESULT=true`` zurueckgeben kann."""
    return bool(
        payload.get("authenticated")
        and payload.get("established")
        and payload.get("connected")
    )


# ---- FastAPI-Dependencies ----


def get_cp_lifecycle() -> AuthLifecycle:
    """Wird in main.py per `app.dependency_overrides` auf den Singleton gemappt."""
    raise RuntimeError(
        "get_cp_lifecycle muss in der App per dependency_overrides gesetzt werden"
    )


def require_session_ok(
    lifecycle: Annotated[AuthLifecycle, Depends(get_cp_lifecycle)],
) -> AuthLifecycle:
    """Endpunkt-Guard: 503 + Retry-After bei AUTH_LOST oder CP_DOWN."""
    snapshot = lifecycle.snapshot()
    if snapshot.auth_status in (AuthStatus.AUTH_LOST, AuthStatus.CP_DOWN):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "auth_lost",
                "message": f"IBKR-Session nicht verfuegbar (status={snapshot.auth_status.value})",
                "retry_after_s": _RETRY_AFTER_S,
                "auth_status": snapshot.auth_status.value,
            },
            headers={"Retry-After": str(_RETRY_AFTER_S)},
        )
    return lifecycle
