"""TWS-Lifecycle: Heartbeat-Loop + Connect-State fuer den TWS-Adapter.

Pendant zu :class:`broker_gateway.cp.lifecycle.AuthLifecycle`, aber gegen
die TWS-Socket-API. Statt eines Tickle-Loops mit Reauth-Mechanik beobachtet
der TWS-Lifecycle nur ``ib.isConnected()`` + ``ib.client.isReady()`` - die
eigentliche Recovery uebernimmt IB Gateway + IBC (Daily-Restart, Sat-
Reset). Karte 33cb35b1.

State-Maschine pro Heartbeat::

    isConnected? --no--> try connect -+--> success -> ready? --yes--> OK
                                       |                       --no---> SESSION_LOST
                                       +--> fail x3 -> TWS_DOWN
    isConnected? --yes-> ready? --yes--> OK
                         ready? --no---> SESSION_LOST

Das Modul bringt seine eigene FastAPI-Dependency mit
(``get_tws_lifecycle``); ``main.py`` verdrahtet sie im
``BG_BACKEND=tws``-Pfad mit einem ``TWSLifecycle``-Singleton.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from broker_gateway.auth_status import AuthStatus

if TYPE_CHECKING:
    from broker_gateway.cp.lifecycle import LifecycleSnapshot
    from broker_gateway.tws.client import TWSClient


logger = logging.getLogger(__name__)


_DEFAULT_HEARTBEAT_INTERVAL_S = 60.0
_DEFAULT_MAX_CONNECT_FAILURES = 3
_DEFAULT_RECOVERY_INTERVAL_S = 10.0
_HEARTBEAT_ENV = "BG_TWS_HEARTBEAT_SEC"
_RECOVERY_ENV = "BG_TWS_RECOVERY_SEC"


def _interval_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("ENV %s=%r ist keine gueltige Zahl, nutze Default %s", name, raw, default)
        return default


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class TWSLifecycleSnapshot:
    """Read-only Sicht auf den aktuellen TWS-Lifecycle-State.

    Felder sind bewusst tws-spezifisch und nicht direkt mit
    :class:`~broker_gateway.cp.lifecycle.LifecycleSnapshot` kompatibel.
    Der Konsumenten-Pfad fuer ``/v1/internal/health`` geht ueber den
    Backend-Adapter in Phase 3.
    """

    auth_status: AuthStatus
    is_connected: bool
    is_ready: bool
    last_heartbeat_at: datetime | None
    last_connect_attempt_at: datetime | None
    last_connect_success_at: datetime | None
    consecutive_connect_failures: int
    client_id: int | None
    session_age_s: float | None


class TWSLifecycle:
    """Heartbeat-Wrapper um einen :class:`~broker_gateway.tws.client.TWSClient`.

    Der Lifecycle besitzt den Client (``connect`` / ``disconnect`` werden
    hier orchestriert) und stellt einen Hintergrund-Task bereit, der alle
    ``heartbeat_interval_s`` ein :meth:`tick_once` ausloest.

    Status-Mapping:

    - Connection steht und ``isReady()`` true → :data:`AuthStatus.OK`.
    - Connection steht, aber ``isReady()`` false → :data:`AuthStatus.SESSION_LOST`.
    - Connection nicht steht und der ``await connect()`` schlaegt
      ``max_connect_failures``-mal in Folge fehl → :data:`AuthStatus.TWS_DOWN`.
    - Initial vor dem ersten erfolgreichen Tick: :data:`AuthStatus.TWS_DOWN`.
    """

    def __init__(
        self,
        client: TWSClient,
        *,
        heartbeat_interval_s: float | None = None,
        recovery_interval_s: float | None = None,
        max_connect_failures: int = _DEFAULT_MAX_CONNECT_FAILURES,
    ) -> None:
        self._client = client
        self.heartbeat_interval_s = (
            heartbeat_interval_s
            if heartbeat_interval_s is not None
            else _interval_from_env(_HEARTBEAT_ENV, _DEFAULT_HEARTBEAT_INTERVAL_S)
        )
        # Im non-OK-Zustand (disconnected / session_lost / tws_down) pollt
        # der Loop schneller, damit ein autonomer Reconnect nach einem
        # harten TWS-Neustart zuegig (< 60s) greift, statt bis zum
        # naechsten Heartbeat zu warten (Karte 6dbf3026). Im OK-Zustand
        # bleibt es beim ruhigen heartbeat_interval_s.
        self.recovery_interval_s = (
            recovery_interval_s
            if recovery_interval_s is not None
            else _interval_from_env(_RECOVERY_ENV, _DEFAULT_RECOVERY_INTERVAL_S)
        )
        if max_connect_failures < 1:
            raise ValueError(
                f"max_connect_failures muss >= 1 sein, war {max_connect_failures}"
            )
        self.max_connect_failures = max_connect_failures

        self._status = AuthStatus.TWS_DOWN
        self._last_heartbeat_at: datetime | None = None
        self._last_connect_attempt_at: datetime | None = None
        self._last_connect_success_at: datetime | None = None
        self._consecutive_connect_failures = 0
        self._session_started_at: float | None = None

        self._task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None

    # ---- oeffentliche Status-Sicht ----

    @property
    def status(self) -> AuthStatus:
        return self._status

    @property
    def client(self) -> TWSClient:
        return self._client

    def snapshot(self) -> TWSLifecycleSnapshot:
        connected = self._client.is_connected()
        ready = bool(connected and self._is_ready())
        if self._session_started_at is None:
            age: float | None = None
        else:
            age = max(0.0, time.monotonic() - self._session_started_at)
        return TWSLifecycleSnapshot(
            auth_status=self._status,
            is_connected=connected,
            is_ready=ready,
            last_heartbeat_at=self._last_heartbeat_at,
            last_connect_attempt_at=self._last_connect_attempt_at,
            last_connect_success_at=self._last_connect_success_at,
            consecutive_connect_failures=self._consecutive_connect_failures,
            client_id=self._client.client_id,
            session_age_s=age,
        )

    # ---- Lifecycle-Steuerung ----

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop_event = asyncio.Event()
        # Erster Heartbeat synchron, damit der Status nach start() schon
        # einen sinnvollen Wert hat (statt initial TWS_DOWN, das nur ein
        # Default vor dem ersten Tick ist).
        await self.tick_once()
        self._task = asyncio.create_task(self._run(), name="tws-heartbeat-loop")

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
        # Hard-Disconnect am Ende - der Lifecycle besitzt die Connection.
        try:
            await self._client.disconnect()
        except Exception:  # noqa: BLE001
            logger.exception(
                "TWSClient.disconnect waehrend Lifecycle-Stop fehlgeschlagen"
            )

    async def __aenter__(self) -> TWSLifecycle:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.stop()

    def _next_interval(self) -> float:
        """Gesamtfrist bis zum naechsten faelligen Heartbeat-Tick.

        OK-Zustand: ruhiges ``heartbeat_interval_s``. Andernfalls das
        kuerzere ``recovery_interval_s`` (nie laenger als der Heartbeat),
        damit ein Reconnect nach TWS-Socket-Abriss schnell nachzieht
        (Karte 6dbf3026).
        """
        if self._status is AuthStatus.OK:
            return self.heartbeat_interval_s
        return min(self.recovery_interval_s, self.heartbeat_interval_s)

    def _poll_step(self) -> float:
        """Schrittweite, in der der OK-Wait das volle Intervall abwartet.

        Der ruhige OK-Wait (``heartbeat_interval_s``, Default 60s) wird in
        Schritten von ``min(recovery_interval_s, heartbeat_interval_s)``
        abgewartet, sodass ein Socket-Abriss innerhalb von
        ``recovery_interval_s`` erkannt wird, statt erst beim naechsten
        regulaeren Heartbeat (Karte 568adcf0). ``is_connected()`` ist ein
        billiger lokaler Check ohne Netzwerk-/IBKR-Traffic, daher ist das
        haeufigere Pruefen unkritisch.
        """
        return min(self.recovery_interval_s, self.heartbeat_interval_s)

    async def _wait_until_due(self) -> bool:
        """Wartet bis zum naechsten faelligen Tick, in ``_poll_step``-Schritten.

        Liefert ``True``, wenn der Loop beendet werden soll (stop_event
        gesetzt), sonst ``False``. Bricht im OK-Zustand frueh ab, sobald
        ``self._client.is_connected()`` ``False`` liefert (harter
        Socket-Abriss): dann zieht in :meth:`_run` sofort ein Tick nach,
        statt das volle ``heartbeat_interval_s`` auszusitzen. Das stop_event
        wird nach jedem Schritt geprueft, damit ``stop()``/Cancel auch
        mitten in einem Mehr-Schritt-Wait prompt greift.
        """
        assert self._stop_event is not None
        remaining = self._next_interval()
        step = self._poll_step()
        while remaining > 0.0:
            wait = min(step, remaining) if step > 0.0 else remaining
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            if self._stop_event.is_set():
                return True
            remaining -= wait
            # Frueher Abbruch nur im stationaeren OK-Betrieb: ein harter
            # Socket-Abriss macht is_connected() sofort False - dann nicht
            # den ruhigen Rest-Heartbeat aussitzen, sondern gleich ticken.
            if self._status is AuthStatus.OK and not self._client.is_connected():
                return False
        return False

    async def _run(self) -> None:
        assert self._stop_event is not None
        try:
            while not self._stop_event.is_set():
                if await self._wait_until_due():
                    break
                if self._stop_event.is_set():
                    break
                await self.tick_once()
        except asyncio.CancelledError:
            raise

    # ---- ein Heartbeat-Zyklus ----

    async def tick_once(self) -> AuthStatus:
        """Ein Heartbeat-Zyklus.

        Nutzbar fuer Tests und manuellen Trigger (analog zu
        ``cp.lifecycle.AuthLifecycle.tick_once``). Liefert den neuen
        Status zurueck.
        """
        self._last_heartbeat_at = _utcnow()
        if not self._client.is_connected():
            await self._try_connect()
            return self._status
        if self._is_ready():
            self._mark_ready()
        else:
            self._status = AuthStatus.SESSION_LOST
        return self._status

    async def _try_connect(self) -> None:
        self._last_connect_attempt_at = _utcnow()
        try:
            await self._client.connect()
        except Exception as exc:  # noqa: BLE001 - Heartbeat darf nicht abbrechen
            logger.warning("TWS-Connect-Versuch fehlgeschlagen: %s", exc)
            self._consecutive_connect_failures += 1
            if self._consecutive_connect_failures >= self.max_connect_failures:
                self._status = AuthStatus.TWS_DOWN
            return
        self._last_connect_success_at = _utcnow()
        # Connect war erfolgreich - direkter Readiness-Check, sonst muesste
        # der Caller einen extra Heartbeat-Zyklus warten.
        if self._is_ready():
            self._mark_ready()
        else:
            self._status = AuthStatus.SESSION_LOST

    def _is_ready(self) -> bool:
        """``True``, wenn ``ib.client.isReady()`` ein True liefert.

        ib_async exponiert ``IB.client.isReady()`` als low-level Indikator
        dafuer, dass connectAsync + initialer Account-Sync abgeschlossen
        sind. Defensive: wenn das innere Attribut nicht vorhanden ist
        (Mock-Pfad), faellt die Pruefung auf ``is_connected()`` zurueck.
        """
        try:
            inner_client = self._client._ib.client  # noqa: SLF001 - low-level Pruefung
        except AttributeError:
            return self._client.is_connected()
        is_ready = getattr(inner_client, "isReady", None)
        if not callable(is_ready):
            return self._client.is_connected()
        try:
            return bool(is_ready())
        except Exception:  # noqa: BLE001 - defensive, niemals den Heartbeat brechen
            return False

    def _mark_ready(self) -> None:
        # Wir wechseln in OK, wenn entweder der Status vorher nicht OK
        # war ODER es noch keinen Session-Start gibt (Default-Initialisierung).
        if self._status is not AuthStatus.OK or self._session_started_at is None:
            self._session_started_at = time.monotonic()
        self._status = AuthStatus.OK
        self._consecutive_connect_failures = 0


# ---- CP-kompatibler Adapter ----


class TWSLifecycleCpAdapter:
    """Wrapper, der einen :class:`TWSLifecycle` ueber dasselbe Interface
    wie :class:`broker_gateway.cp.lifecycle.AuthLifecycle` bereitstellt.

    Hintergrund: Im ``BG_BACKEND=tws``-Pfad (Karte 33cb35b1 Phase 3)
    haengen wir diesen Adapter unter ``app.state.cp_lifecycle`` und
    ueberschreiben den ``get_cp_lifecycle``-Dependency-Slot. Damit
    funktionieren alle Endpunkte (``require_session_ok``,
    ``/v1/internal/health``, ``/v1/status`` usw.) ohne Refactor weiter
    - sie sehen einen ``snapshot()``, der einen ``LifecycleSnapshot``
    mit cp-kompatiblen Feldern liefert.

    cp-spezifische Felder sind:

    - ``cp_reachable`` ↔ ``is_connected``
    - ``last_tickle_at`` ↔ ``last_heartbeat_at``
    - ``last_login_at`` ↔ ``last_connect_success_at``
    - ``consecutive_reauth_failures`` ↔ ``consecutive_connect_failures``
    - ``accounts_initialized`` ↔ ``is_ready``
    - alle Reauth-/SSO-/Bridge-/Auto-Login-Felder bleiben ``None`` /
      Default — sie haben kein TWS-Pendant.

    Folge-Karte 6 (Hard-Cutover) entfernt diesen Adapter, sobald der
    cp-Pfad aus dem Repo verschwindet.
    """

    def __init__(self, tws_lifecycle: TWSLifecycle) -> None:
        self._tws = tws_lifecycle

    @property
    def status(self) -> AuthStatus:
        return self._tws.status

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def tws_lifecycle(self) -> TWSLifecycle:
        return self._tws

    def snapshot(self) -> LifecycleSnapshot:
        from broker_gateway.cp.lifecycle import LifecycleSnapshot

        snap = self._tws.snapshot()
        return LifecycleSnapshot(
            auth_status=snap.auth_status,
            cp_reachable=snap.is_connected,
            last_tickle_at=snap.last_heartbeat_at,
            last_reauth_at=None,
            last_sso_validate_at=None,
            last_login_at=snap.last_connect_success_at,
            session_age_s=snap.session_age_s,
            consecutive_reauth_failures=snap.consecutive_connect_failures,
            accounts_initialized=snap.is_ready,
            session_id=None,
            iserver_bridge_ok=None,
            last_bridge_probe_at=None,
            consecutive_bridge_failures=0,
        )

    async def start(self) -> None:
        await self._tws.start()

    async def stop(self) -> None:
        await self._tws.stop()


# ---- FastAPI-Dependency ----


def get_tws_lifecycle() -> TWSLifecycle:
    """In ``main.py`` per ``app.dependency_overrides`` auf den Singleton gemappt."""
    raise RuntimeError(
        "get_tws_lifecycle muss in der App per dependency_overrides gesetzt werden"
    )


__all__ = [
    "TWSLifecycle",
    "TWSLifecycleCpAdapter",
    "TWSLifecycleSnapshot",
    "get_tws_lifecycle",
]
