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
    ) -> None:
        self._client = client
        self.tickle_interval_s = (
            tickle_interval_s
            if tickle_interval_s is not None
            else _interval_from_env("BG_CP_TICKLE_INTERVAL_S", _DEFAULT_TICKLE_INTERVAL_S)
        )
        self.reauth_max_retries = reauth_max_retries
        self.reauth_backoff_s = reauth_backoff_s

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
        )

    # ---- Lifecycle-Steuerung ----

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        # Erster Tickle synchron, damit der Status beim Startup sinnvoll ist.
        await self.tick_once()
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
        except asyncio.CancelledError:
            raise

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
