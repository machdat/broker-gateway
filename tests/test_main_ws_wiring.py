"""Tests fuer die WS-Lifespan-Verdrahtung in main._build_subscription_layer
(AP-11 K9).

Drei Pfade werden geprueft:

1. Default (BG_QUOTES_SOURCE nicht gesetzt) -> polling-Manager,
   StatusProbe ohne Registry/WS-Client.
2. BG_QUOTES_SOURCE=ws + Lifecycle ok + session_id != None -> ws-Manager,
   StatusProbe mit Registry und ws_reconnect_attempt-Hook.
3. BG_QUOTES_SOURCE=ws aber Lifecycle nicht OK / session_id None ->
   Fallback polling, eine Warning im Log.

Der CPWebSocketClient wird ueber den connect_factory-Pfad mit einer
``FakeConnection`` aus tests/test_ws_client.py (FakeConnection-Pattern)
ersetzt - keine echte Socket-Verbindung. Damit laeuft der Test offline
und in Sub-Sekunden-Zeit.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from broker_gateway.cp.calendar import CalendarService
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import InstrumentDetail, InstrumentsService
from broker_gateway.cp.lifecycle import AuthLifecycle, AuthStatus, LifecycleSnapshot
from broker_gateway.cp.ws_client import CPWebSocketClient
from broker_gateway.main import _build_subscription_layer
from broker_gateway.streams.manager import SubscriptionManager
from broker_gateway.streams.ws_source import WSPushSource


# ---- Test-Fakes ---------------------------------------------------------


class _FakeWSConnection:
    """Minimaler WSConnection-Stub, der nach Auth-Frame ein sts-Ack
    zurueckliefert und sonst blockiert."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._inbox: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)
        if message and message.startswith("{"):
            ack = json.dumps(
                {
                    "topic": "sts",
                    "args": {"connected": True, "authenticated": True},
                }
            )
            await self._inbox.put(ack)

    async def recv(self) -> str:
        return await self._inbox.get()

    async def close(self) -> None:
        self.closed = True


async def _fake_connect_factory(url: str, cookies: str | None) -> _FakeWSConnection:
    return _FakeWSConnection()


class _FakeAuthLifecycle:
    """Minimaler AuthLifecycle-Ersatz: liefert nur einen Snapshot."""

    def __init__(self, *, status: AuthStatus, session_id: str | None) -> None:
        self._status = status
        self._session_id = session_id

    def snapshot(self) -> LifecycleSnapshot:
        return LifecycleSnapshot(
            auth_status=self._status,
            cp_reachable=True,
            last_tickle_at=datetime.now(timezone.utc),
            last_reauth_at=None,
            last_sso_validate_at=None,
            last_login_at=None,
            session_age_s=0.0,
            consecutive_reauth_failures=0,
            accounts_initialized=True,
            session_id=self._session_id,
        )


@dataclass
class _FakeServicesClient:
    """Stub fuer den CPGatewayClient-Cookie-Lookup. Liefert nichts."""

    class _HttpxStub:
        cookies = type("C", (), {"jar": []})()

    _client = _HttpxStub()


class _FakeInstrumentsService:
    async def info(self, conid: int) -> InstrumentDetail:
        return InstrumentDetail(
            conid=conid,
            symbol="AAPL",
            description="Apple Inc.",
            exchange_id="NASDAQ",
            calendar_url="/v1/exchanges/NASDAQ/calendar",
        )


class _FakeCalendarService:
    pass


# ---- Tests --------------------------------------------------------------


async def test_default_polling_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ohne BG_QUOTES_SOURCE: Manager polling, StatusProbe ohne Registry."""
    monkeypatch.delenv("BG_QUOTES_SOURCE", raising=False)
    sub_manager, status_probe, ws_client, ws_source = (
        await _build_subscription_layer(
            services_client=_FakeServicesClient(),  # type: ignore[arg-type]
            cp_lifecycle=_FakeAuthLifecycle(
                status=AuthStatus.OK, session_id="sid"
            ),  # type: ignore[arg-type]
            inst_service=_FakeInstrumentsService(),  # type: ignore[arg-type]
            cal_service=_FakeCalendarService(),  # type: ignore[arg-type]
            override_manager=None,
        )
    )
    assert sub_manager.quotes_source == "polling"
    assert ws_client is None
    assert ws_source is None
    # Ohne Registry liefert die Probe subscriptions_active=0.
    snap = status_probe.snapshot(
        _FakeAuthLifecycle(status=AuthStatus.OK, session_id="sid")  # type: ignore[arg-type]
    )
    assert snap["subscriptions_active"] == 0
    assert snap["reconnect_attempt"] == 0


async def test_ws_path_when_session_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mit BG_QUOTES_SOURCE=ws + OK-Session: Manager ws, Probe mit Registry."""
    monkeypatch.setenv("BG_QUOTES_SOURCE", "ws")
    # Patch des WS-Connect-Factories an der Klasse, damit der Lifespan-
    # interne CPWebSocketClient() den Fake bekommt.
    monkeypatch.setattr(
        "broker_gateway.main.CPWebSocketClient",
        lambda: CPWebSocketClient(connect_factory=_fake_connect_factory),
    )
    sub_manager, status_probe, ws_client, ws_source = (
        await _build_subscription_layer(
            services_client=_FakeServicesClient(),  # type: ignore[arg-type]
            cp_lifecycle=_FakeAuthLifecycle(
                status=AuthStatus.OK, session_id="sid-xyz"
            ),  # type: ignore[arg-type]
            inst_service=_FakeInstrumentsService(),  # type: ignore[arg-type]
            cal_service=_FakeCalendarService(),  # type: ignore[arg-type]
            override_manager=None,
        )
    )
    try:
        assert sub_manager.quotes_source == "ws"
        assert isinstance(ws_client, CPWebSocketClient)
        assert isinstance(ws_source, WSPushSource)
        snap = status_probe.snapshot(
            _FakeAuthLifecycle(status=AuthStatus.OK, session_id="sid-xyz")  # type: ignore[arg-type]
        )
        # Registry ist initial leer.
        assert snap["subscriptions_active"] == 0
        # reconnect_attempt-Hook ist verdrahtet (gibt aktuelle Counter-
        # Sicht des WS-Clients zurueck, hier 0).
        assert snap["reconnect_attempt"] == 0
    finally:
        if ws_source is not None:
            await ws_source.stop()
        if ws_client is not None:
            await ws_client.aclose()


async def test_ws_path_falls_back_when_session_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """BG_QUOTES_SOURCE=ws aber Session nicht OK -> polling + Warning."""
    monkeypatch.setenv("BG_QUOTES_SOURCE", "ws")
    with caplog.at_level(logging.WARNING, logger="broker_gateway.main"):
        sub_manager, status_probe, ws_client, ws_source = (
            await _build_subscription_layer(
                services_client=_FakeServicesClient(),  # type: ignore[arg-type]
                cp_lifecycle=_FakeAuthLifecycle(
                    status=AuthStatus.AUTH_LOST, session_id=None
                ),  # type: ignore[arg-type]
                inst_service=_FakeInstrumentsService(),  # type: ignore[arg-type]
                cal_service=_FakeCalendarService(),  # type: ignore[arg-type]
                override_manager=None,
            )
        )
    assert sub_manager.quotes_source == "polling"
    assert ws_client is None
    assert ws_source is None
    assert any(
        "BG_QUOTES_SOURCE=ws" in record.message
        and "fallback polling" in record.message
        for record in caplog.records
    )


async def test_override_manager_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wenn der Aufrufer einen Manager injiziert, wird kein WS gestartet."""
    monkeypatch.setenv("BG_QUOTES_SOURCE", "ws")

    class _MarkerManager(SubscriptionManager):
        pass

    custom = _MarkerManager(
        _FakeServicesClient(),  # type: ignore[arg-type]
    )
    sub_manager, status_probe, ws_client, ws_source = (
        await _build_subscription_layer(
            services_client=_FakeServicesClient(),  # type: ignore[arg-type]
            cp_lifecycle=_FakeAuthLifecycle(
                status=AuthStatus.OK, session_id="sid"
            ),  # type: ignore[arg-type]
            inst_service=_FakeInstrumentsService(),  # type: ignore[arg-type]
            cal_service=_FakeCalendarService(),  # type: ignore[arg-type]
            override_manager=custom,
        )
    )
    assert sub_manager is custom
    assert ws_client is None
    assert ws_source is None
