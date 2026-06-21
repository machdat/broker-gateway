"""Tests fuer broker_gateway.tws.lifecycle (Karte 33cb35b1 Phase 4).

Coverage-Ziel: >=90% fuer src/broker_gateway/tws/lifecycle.py.

Mock-Strategie: TWSClient wird komplett gemockt. ``is_connected()``
und ``connect()`` werden pro Test gesteuert; das innere ``_ib.client.
isReady()``-Attribut wird via SimpleNamespace nachgebaut, damit
``TWSLifecycle._is_ready()`` den richtigen Pfad nimmt.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from broker_gateway.auth_status import AuthStatus
from broker_gateway.cp.lifecycle import LifecycleSnapshot
from broker_gateway.tws.lifecycle import (
    TWSLifecycle,
    TWSLifecycleCpAdapter,
    TWSLifecycleSnapshot,
    get_tws_lifecycle,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


def _make_client(
    *,
    connected: bool = False,
    ready: bool = False,
    connect_raises: BaseException | None = None,
    client_id: int | None = None,
) -> MagicMock:
    """Baut einen TWSClient-Mock mit konfigurierbarem Connect-/Ready-State.

    Defensive: ``_ib.client.isReady()`` wird ueber SimpleNamespace gemockt,
    sodass ``TWSLifecycle._is_ready`` den ib_async-Pfad sieht.
    """
    client = MagicMock()
    client.is_connected = MagicMock(return_value=connected)
    if connect_raises is not None:
        client.connect = AsyncMock(side_effect=connect_raises)
    else:
        client.connect = AsyncMock(return_value=None)
    client.disconnect = AsyncMock(return_value=None)
    client.client_id = client_id
    client._ib = SimpleNamespace(
        client=SimpleNamespace(isReady=lambda: ready)
    )
    return client


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------


class TestSnapshot:
    async def test_snapshot_initial_state_is_tws_down(self) -> None:
        client = _make_client(connected=False)
        lifecycle = TWSLifecycle(client)
        snap = lifecycle.snapshot()
        assert snap.auth_status is AuthStatus.TWS_DOWN
        assert snap.is_connected is False
        assert snap.is_ready is False
        assert snap.consecutive_connect_failures == 0
        assert snap.session_age_s is None
        assert snap.last_heartbeat_at is None

    async def test_snapshot_after_successful_tick(self) -> None:
        client = _make_client(connected=True, ready=True, client_id=101)
        lifecycle = TWSLifecycle(client)
        await lifecycle.tick_once()
        snap = lifecycle.snapshot()
        assert snap.auth_status is AuthStatus.OK
        assert snap.is_connected is True
        assert snap.is_ready is True
        assert snap.client_id == 101
        assert snap.session_age_s is not None
        assert snap.session_age_s >= 0.0
        assert snap.last_heartbeat_at is not None

    async def test_snapshot_session_lost_when_connected_but_not_ready(
        self,
    ) -> None:
        client = _make_client(connected=True, ready=False)
        lifecycle = TWSLifecycle(client)
        await lifecycle.tick_once()
        snap = lifecycle.snapshot()
        assert snap.auth_status is AuthStatus.SESSION_LOST
        assert snap.is_ready is False


# --------------------------------------------------------------------------
# tick_once - State-Uebergaenge
# --------------------------------------------------------------------------


class TestTickOnce:
    async def test_tick_when_connected_and_ready_returns_ok(self) -> None:
        client = _make_client(connected=True, ready=True)
        lifecycle = TWSLifecycle(client)
        result = await lifecycle.tick_once()
        assert result is AuthStatus.OK
        client.connect.assert_not_awaited()  # bereits verbunden

    async def test_tick_when_disconnected_calls_connect(self) -> None:
        client = _make_client(connected=False, ready=True)
        # nach connect ist die Connection hergestellt - is_connected
        # liefert dann True. Das emuliert ib_async.connectAsync().
        connect_calls = {"count": 0}

        async def _connect() -> None:
            connect_calls["count"] += 1
            client.is_connected.return_value = True

        client.connect = AsyncMock(side_effect=_connect)
        lifecycle = TWSLifecycle(client)
        result = await lifecycle.tick_once()
        assert result is AuthStatus.OK
        assert connect_calls["count"] == 1

    async def test_connect_failure_increments_counter_keeps_status(
        self,
    ) -> None:
        client = _make_client(
            connected=False,
            ready=False,
            connect_raises=RuntimeError("connection refused"),
        )
        lifecycle = TWSLifecycle(client, max_connect_failures=3)
        result = await lifecycle.tick_once()
        # Erster Fehlversuch: counter=1, Status bleibt initial TWS_DOWN
        # aber NICHT durch die max-failures-Branche.
        assert result is AuthStatus.TWS_DOWN
        assert lifecycle.snapshot().consecutive_connect_failures == 1

    async def test_three_connect_failures_in_a_row_set_tws_down(
        self,
    ) -> None:
        client = _make_client(
            connected=False,
            connect_raises=RuntimeError("nope"),
        )
        lifecycle = TWSLifecycle(client, max_connect_failures=3)
        for _ in range(3):
            await lifecycle.tick_once()
        snap = lifecycle.snapshot()
        assert snap.auth_status is AuthStatus.TWS_DOWN
        assert snap.consecutive_connect_failures == 3

    async def test_successful_connect_after_failure_resets_counter(
        self,
    ) -> None:
        client = _make_client(connected=False, ready=True)
        # Erster Versuch failt, zweiter klappt
        attempts = {"count": 0}

        async def _flaky_connect() -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise RuntimeError("transient")
            client.is_connected.return_value = True

        client.connect = AsyncMock(side_effect=_flaky_connect)
        lifecycle = TWSLifecycle(client, max_connect_failures=3)
        await lifecycle.tick_once()  # fail
        assert lifecycle.snapshot().consecutive_connect_failures == 1
        await lifecycle.tick_once()  # success
        snap = lifecycle.snapshot()
        assert snap.auth_status is AuthStatus.OK
        assert snap.consecutive_connect_failures == 0

    async def test_session_lost_when_connect_succeeds_but_not_ready(
        self,
    ) -> None:
        client = _make_client(connected=False, ready=False)

        async def _connect() -> None:
            client.is_connected.return_value = True

        client.connect = AsyncMock(side_effect=_connect)
        lifecycle = TWSLifecycle(client)
        result = await lifecycle.tick_once()
        # Connect klappt, aber isReady() liefert false → session_lost
        assert result is AuthStatus.SESSION_LOST

    async def test_session_lost_then_ready_recovers_to_ok(self) -> None:
        # Server kickt die Session: isConnected bleibt true, isReady
        # springt von true → false → true.
        ready_state = {"v": True}
        client = MagicMock()
        client.is_connected = MagicMock(return_value=True)
        client.connect = AsyncMock(return_value=None)
        client.disconnect = AsyncMock(return_value=None)
        client.client_id = 100
        client._ib = SimpleNamespace(
            client=SimpleNamespace(isReady=lambda: ready_state["v"])
        )
        lifecycle = TWSLifecycle(client)
        await lifecycle.tick_once()  # OK
        assert lifecycle.status is AuthStatus.OK
        ready_state["v"] = False
        await lifecycle.tick_once()  # SESSION_LOST
        assert lifecycle.status is AuthStatus.SESSION_LOST
        ready_state["v"] = True
        await lifecycle.tick_once()  # OK
        assert lifecycle.status is AuthStatus.OK

    async def test_reconnects_after_socket_drop_following_ok(self) -> None:
        """Karte 6dbf3026: nach einem OK-Zustand reisst der TWS-Socket ab
        (is_connected springt auf False, z.B. TWS-Prozess-Neustart). Der
        naechste Heartbeat ruft connect() und kehrt nach erfolgreichem
        Reconnect autonom zu OK zurueck - ohne externen API-Restart."""
        states = {"connected": True}
        client = MagicMock()
        client.is_connected = MagicMock(side_effect=lambda: states["connected"])

        async def _connect() -> None:
            states["connected"] = True

        client.connect = AsyncMock(side_effect=_connect)
        client.disconnect = AsyncMock(return_value=None)
        client.client_id = 100
        client._ib = SimpleNamespace(
            client=SimpleNamespace(isReady=lambda: True)
        )
        lifecycle = TWSLifecycle(client)
        await lifecycle.tick_once()  # OK
        assert lifecycle.status is AuthStatus.OK
        # Harter Socket-Abriss: is_connected False, ohne dass disconnect lief.
        states["connected"] = False
        await lifecycle.tick_once()  # erkennt Abriss -> connect() -> OK
        assert lifecycle.status is AuthStatus.OK
        client.connect.assert_awaited()


# --------------------------------------------------------------------------
# is_ready Defensive
# --------------------------------------------------------------------------


class TestIsReadyDefensive:
    async def test_is_ready_falls_back_to_is_connected_when_no_inner_attr(
        self,
    ) -> None:
        client = MagicMock()
        client.is_connected = MagicMock(return_value=True)
        client.connect = AsyncMock(return_value=None)
        client.disconnect = AsyncMock(return_value=None)
        client.client_id = 100
        # Kein _ib.client.isReady - simuliert minimalen Mock
        del client._ib
        lifecycle = TWSLifecycle(client)
        await lifecycle.tick_once()
        # isReady-Fallback liefert is_connected=True → OK
        assert lifecycle.status is AuthStatus.OK

    async def test_is_ready_returns_false_on_inner_exception(self) -> None:
        client = _make_client(connected=True, ready=True)

        def _raises() -> bool:
            raise RuntimeError("ib internal")

        client._ib.client.isReady = _raises
        lifecycle = TWSLifecycle(client)
        result = await lifecycle.tick_once()
        # Defensive: isReady raised → wir sehen False → SESSION_LOST
        assert result is AuthStatus.SESSION_LOST


# --------------------------------------------------------------------------
# start / stop / Heartbeat-Loop
# --------------------------------------------------------------------------


class TestStartStop:
    async def test_start_creates_task_and_runs_initial_tick(self) -> None:
        client = _make_client(connected=True, ready=True)
        lifecycle = TWSLifecycle(client, heartbeat_interval_s=10.0)
        await lifecycle.start()
        try:
            # Initialer Tick wurde synchron ausgefuehrt
            assert lifecycle.status is AuthStatus.OK
            assert lifecycle._task is not None
        finally:
            await lifecycle.stop()

    async def test_start_is_idempotent(self) -> None:
        client = _make_client(connected=True, ready=True)
        lifecycle = TWSLifecycle(client, heartbeat_interval_s=10.0)
        await lifecycle.start()
        first_task = lifecycle._task
        await lifecycle.start()
        try:
            assert lifecycle._task is first_task
        finally:
            await lifecycle.stop()

    async def test_stop_cancels_task_and_disconnects_client(self) -> None:
        client = _make_client(connected=True, ready=True)
        lifecycle = TWSLifecycle(client, heartbeat_interval_s=10.0)
        await lifecycle.start()
        await lifecycle.stop()
        assert lifecycle._task is None
        client.disconnect.assert_awaited()

    async def test_stop_tolerates_disconnect_errors(self) -> None:
        client = _make_client(connected=True, ready=True)
        client.disconnect = AsyncMock(side_effect=RuntimeError("boom"))
        lifecycle = TWSLifecycle(client, heartbeat_interval_s=10.0)
        await lifecycle.start()
        # darf nicht raisen
        await lifecycle.stop()

    async def test_aenter_aexit_drives_lifecycle(self) -> None:
        client = _make_client(connected=True, ready=True)
        lifecycle = TWSLifecycle(client, heartbeat_interval_s=10.0)
        async with lifecycle as lc:
            assert lc is lifecycle
            assert lc.status is AuthStatus.OK
        client.disconnect.assert_awaited()

    async def test_heartbeat_loop_fires_periodically(self) -> None:
        client = _make_client(connected=True, ready=True)
        lifecycle = TWSLifecycle(client, heartbeat_interval_s=0.05)
        await lifecycle.start()
        try:
            # 1 initialer Tick + ~3 Heartbeats in 0.2s
            await asyncio.sleep(0.2)
            assert lifecycle.snapshot().last_heartbeat_at is not None
        finally:
            await lifecycle.stop()


# --------------------------------------------------------------------------
# Konfiguration / Validation
# --------------------------------------------------------------------------


class TestConfig:
    def test_invalid_max_connect_failures_raises(self) -> None:
        client = _make_client()
        with pytest.raises(ValueError):
            TWSLifecycle(client, max_connect_failures=0)

    def test_heartbeat_interval_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BG_TWS_HEARTBEAT_SEC", "12.5")
        client = _make_client()
        lifecycle = TWSLifecycle(client)
        assert lifecycle.heartbeat_interval_s == 12.5

    def test_heartbeat_interval_invalid_env_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BG_TWS_HEARTBEAT_SEC", "not-a-number")
        client = _make_client()
        lifecycle = TWSLifecycle(client)
        assert lifecycle.heartbeat_interval_s == 60.0  # Default

    def test_explicit_heartbeat_interval_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BG_TWS_HEARTBEAT_SEC", "12.5")
        client = _make_client()
        lifecycle = TWSLifecycle(client, heartbeat_interval_s=99.0)
        assert lifecycle.heartbeat_interval_s == 99.0

    def test_status_property(self) -> None:
        client = _make_client()
        lifecycle = TWSLifecycle(client)
        assert lifecycle.status is AuthStatus.TWS_DOWN

    def test_client_property(self) -> None:
        client = _make_client()
        lifecycle = TWSLifecycle(client)
        assert lifecycle.client is client


# --------------------------------------------------------------------------
# Recovery-Intervall (Karte 6dbf3026)
# --------------------------------------------------------------------------


class TestRecoveryInterval:
    def test_default_recovery_interval(self) -> None:
        client = _make_client()
        lifecycle = TWSLifecycle(client)
        assert lifecycle.recovery_interval_s == 10.0

    def test_recovery_interval_override(self) -> None:
        client = _make_client()
        lifecycle = TWSLifecycle(client, recovery_interval_s=3.0)
        assert lifecycle.recovery_interval_s == 3.0

    def test_recovery_interval_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BG_TWS_RECOVERY_SEC", "7.5")
        client = _make_client()
        lifecycle = TWSLifecycle(client)
        assert lifecycle.recovery_interval_s == 7.5

    def test_explicit_recovery_interval_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BG_TWS_RECOVERY_SEC", "7.5")
        client = _make_client()
        lifecycle = TWSLifecycle(client, recovery_interval_s=2.0)
        assert lifecycle.recovery_interval_s == 2.0

    def test_next_interval_ok_uses_heartbeat(self) -> None:
        client = _make_client()
        lifecycle = TWSLifecycle(
            client, heartbeat_interval_s=60.0, recovery_interval_s=10.0
        )
        lifecycle._status = AuthStatus.OK
        assert lifecycle._next_interval() == 60.0

    def test_next_interval_not_ok_uses_recovery(self) -> None:
        client = _make_client()
        lifecycle = TWSLifecycle(
            client, heartbeat_interval_s=60.0, recovery_interval_s=10.0
        )
        for status in (AuthStatus.SESSION_LOST, AuthStatus.TWS_DOWN):
            lifecycle._status = status
            assert lifecycle._next_interval() == 10.0

    def test_next_interval_recovery_capped_at_heartbeat(self) -> None:
        # recovery > heartbeat: nie laenger warten als der Heartbeat.
        client = _make_client()
        lifecycle = TWSLifecycle(
            client, heartbeat_interval_s=5.0, recovery_interval_s=30.0
        )
        lifecycle._status = AuthStatus.TWS_DOWN
        assert lifecycle._next_interval() == 5.0


# --------------------------------------------------------------------------
# CP-Adapter
# --------------------------------------------------------------------------


class TestCpAdapter:
    async def test_adapter_snapshot_has_cp_compatible_shape(self) -> None:
        # Vollstaendiger Pfad: nicht verbunden → connect → OK. Damit
        # ist last_connect_success_at gesetzt, was der Adapter auf
        # last_login_at mappt.
        client = _make_client(connected=False, ready=True, client_id=100)

        async def _connect() -> None:
            client.is_connected.return_value = True

        client.connect = AsyncMock(side_effect=_connect)
        lifecycle = TWSLifecycle(client)
        await lifecycle.tick_once()
        adapter = TWSLifecycleCpAdapter(lifecycle)
        snap = adapter.snapshot()
        # Liefert ein cp.lifecycle.LifecycleSnapshot
        assert isinstance(snap, LifecycleSnapshot)
        assert snap.auth_status is AuthStatus.OK
        assert snap.cp_reachable is True  # entspricht is_connected
        assert snap.accounts_initialized is True  # entspricht is_ready
        assert snap.last_login_at is not None
        # cp-spezifische Felder sind None / Default
        assert snap.last_reauth_at is None
        assert snap.last_sso_validate_at is None
        assert snap.session_id is None
        assert snap.iserver_bridge_ok is None
        assert snap.consecutive_bridge_failures == 0
        assert snap.last_auto_login_attempt_at is None

    async def test_adapter_snapshot_when_tws_down(self) -> None:
        client = _make_client(
            connected=False,
            connect_raises=RuntimeError("nope"),
        )
        lifecycle = TWSLifecycle(client, max_connect_failures=1)
        await lifecycle.tick_once()
        adapter = TWSLifecycleCpAdapter(lifecycle)
        snap = adapter.snapshot()
        assert snap.auth_status is AuthStatus.TWS_DOWN
        assert snap.cp_reachable is False
        assert snap.consecutive_reauth_failures == 1

    async def test_adapter_status_property_passthrough(self) -> None:
        client = _make_client(connected=True, ready=True)
        lifecycle = TWSLifecycle(client)
        await lifecycle.tick_once()
        adapter = TWSLifecycleCpAdapter(lifecycle)
        assert adapter.status is AuthStatus.OK
        assert adapter.session_id is None
        assert adapter.tws_lifecycle is lifecycle

    async def test_adapter_start_stop_delegates(self) -> None:
        client = _make_client(connected=True, ready=True)
        lifecycle = TWSLifecycle(client, heartbeat_interval_s=10.0)
        adapter = TWSLifecycleCpAdapter(lifecycle)
        await adapter.start()
        try:
            assert lifecycle.status is AuthStatus.OK
        finally:
            await adapter.stop()
        client.disconnect.assert_awaited()


# --------------------------------------------------------------------------
# FastAPI-Dependency
# --------------------------------------------------------------------------


class TestDependency:
    def test_get_tws_lifecycle_raises_without_override(self) -> None:
        with pytest.raises(RuntimeError, match="dependency_overrides"):
            get_tws_lifecycle()
