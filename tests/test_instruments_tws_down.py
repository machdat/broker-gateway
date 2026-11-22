"""Regressionstest BRO-0158: TWS-Down-Pfad von ``GET /v1/instruments/{conid}``.

Beobachtet (trading_robot-Bot-Log 2026-07-24 21:45 UTC): Im nächtlichen
IBKR-Wartungsfenster lieferte der Endpunkt für einen zuvor abgefragten
conid (14079) HTTP 500, statt das zuletzt gecachte ``InstrumentDetail``
auszuliefern. Ursache: abgelaufener Detail-Cache (1-h-Hours-TTL, Karte
35162b2d) + Live-Call gegen den abgerissenen TWS-Socket → ib_async
wirft ``ConnectionError``, uncaught → 500. Später im Fenster, wenn der
Lifecycle-Status auf ``TWS_DOWN`` umgeschaltet ist, antwortete der
Session-Guard mit 503 ``auth_lost`` — ebenfalls statt des Cache-Fallbacks.

Die Route wird hier gegen die komplette App (``BG_BACKEND=tws``)
getestet. Der TWS-Client erhält einen Fake-Connect; sein ``_ib``-Handle
ist eine echte, aber nie verbundene ``IB()``-Instanz — jeder Live-Fetch
scheitert damit mit ``ConnectionError``, exakt wie im Wartungsfenster.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.auth_status import AuthStatus
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import InstrumentDetail, InstrumentsService
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app
from broker_gateway.tws.instruments import TWSInstrumentsService


_ADMIN_VALUE = "tws-down-regression-admin-token-aaaaaaaa"


def _detail() -> InstrumentDetail:
    return InstrumentDetail(
        conid=14079,
        symbol="SAP",
        company_name="SAP SE",
        currency="EUR",
        sec_type="STK",
        exchange="XETR",
        exchange_id="XETR",
        calendar_url="/v1/exchanges/XETR/calendar",
        trading_hours="20260724:0400-20260724:2000",
        liquid_hours="20260724:0930-20260724:1600",
        time_zone_id="Europe/Berlin",
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_VALUE}"}


def _stub_details(detail: InstrumentDetail) -> SimpleNamespace:
    """Mimt einen ``ContractDetails``-Eintrag der ib_async-Antwort."""
    contract = SimpleNamespace(
        conId=detail.conid,
        symbol=detail.symbol,
        primaryExchange=detail.exchange_id,
        exchange=detail.exchange,
        currency=detail.currency,
        secType=detail.sec_type,
    )
    return SimpleNamespace(
        contract=contract,
        longName=detail.company_name,
        tradingHours=detail.trading_hours or "",
        liquidHours=detail.liquid_hours or "",
        timeZoneId=detail.time_zone_id or "",
    )


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(
        Token(
            value=_ADMIN_VALUE,
            caller_id="regression-admin",
            scopes=[SCOPE_ADMIN_ALL],
        )
    )
    return s


@pytest.fixture(autouse=True)
def tws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_STACK_KIND", "paper")
    monkeypatch.setenv("BG_BOOTSTRAP_ADMIN_TOKEN", _ADMIN_VALUE)


def _install_fake_tws_client(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, bool]:
    """Fake-Connect für den owned TWSClient; Zustand steuerbar pro Test."""
    state: dict[str, bool] = {"connected": False, "fail_connect": False}

    async def _fake_connect(self: Any) -> None:
        if state["fail_connect"]:
            raise ConnectionError("TWS down (Wartungsfenster)")
        state["connected"] = True
        self._client_id = 100

    async def _fake_disconnect(self: Any) -> None:
        state["connected"] = False

    def _fake_is_connected(self: Any) -> bool:
        return state["connected"]

    monkeypatch.setattr(
        "broker_gateway.tws.client.TWSClient.connect", _fake_connect
    )
    monkeypatch.setattr(
        "broker_gateway.tws.client.TWSClient.disconnect", _fake_disconnect
    )
    monkeypatch.setattr(
        "broker_gateway.tws.client.TWSClient.is_connected", _fake_is_connected
    )
    return state


def _make_tws_app(
    store: InMemoryTokenStore, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, dict[str, bool]]:
    monkeypatch.setenv("BG_BACKEND", "tws")
    state = _install_fake_tws_client(monkeypatch)
    app = create_app(store=store)
    return TestClient(app), state


def _tws_lifecycle(client: TestClient) -> Any:
    return client.app.state.cp_lifecycle.tws_lifecycle


class TestTwsDownRoute:
    """Verifikation BRO-0158 auf Endpoint-Ebene (BG_BACKEND=tws)."""

    def test_down_serves_cached_detail_with_200(
        self,
        store: InMemoryTokenStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verifikation 1: während TWS-Down liefert der Endpunkt für einen
        zuvor abgefragten conid 200 aus dem Cache (statt 500)."""
        client, _ = _make_tws_app(store, monkeypatch)
        with client:
            service = client.app.state.instruments_service
            assert isinstance(service, TWSInstrumentsService)
            # „Zuvor abgefragt": der frische Hours-Cache (1 h) ist im
            # Fenster abgelaufen, der Last-Known-Good-Store nicht.
            service.info_stale_cache.set(14079, _detail())

            response = client.get("/v1/instruments/14079", headers=_auth())
            assert response.status_code == 200
            body = response.json()
            assert body["conid"] == 14079
            assert body["symbol"] == "SAP"
            assert body["trading_hours"] == "20260724:0400-20260724:2000"
            assert body["liquid_hours"] == "20260724:0930-20260724:1600"
            assert body["time_zone_id"] == "Europe/Berlin"

    def test_down_cold_cache_returns_503_tws_unavailable(
        self,
        store: InMemoryTokenStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verifikation 2: kein Cache-Eintrag → 503 (nicht 500) mit
        Diagnose, klarem Code und Retry-After."""
        client, _ = _make_tws_app(store, monkeypatch)
        with client:
            response = client.get("/v1/instruments/14079", headers=_auth())
            assert response.status_code == 503
            body = response.json()
            assert body["error"]["code"] == "tws_unavailable"
            assert "14079" in body["error"]["message"]
            assert body["error"]["retry_after_s"] == 30
            assert body["error"]["extra"]["diagnostic"]
            assert response.headers["retry-after"] == "30"

    def test_down_lifecycle_tws_down_still_serves_cache(
        self,
        store: InMemoryTokenStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Entscheidender Fall: auch im Lifecycle-Status ``TWS_DOWN``
        (der Session-Guard würde 503 ``auth_lost`` liefern) antwortet der
        Endpunkt 200 aus dem Cache — statische Contract-Details dürfen
        das Wartungsfenster überstehen (Soll-Zustand 1)."""
        client, state = _make_tws_app(store, monkeypatch)
        with client:
            service = client.app.state.instruments_service
            service.info_stale_cache.set(14079, _detail())

            # TWS geht unter, Reconnect schlägt dreimal fehl.
            state["connected"] = False
            state["fail_connect"] = True
            tws_lifecycle = _tws_lifecycle(client)
            for _ in range(3):
                asyncio.run(tws_lifecycle.tick_once())
            assert tws_lifecycle.status is AuthStatus.TWS_DOWN

            response = client.get("/v1/instruments/14079", headers=_auth())
            assert response.status_code == 200
            assert response.json()["symbol"] == "SAP"

    def test_outside_window_live_fetch_unchanged(
        self,
        store: InMemoryTokenStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Verifikation 3: außerhalb des Fensters (Live-Fetch klappt)
        bleibt das Verhalten unverändert — 200 aus dem Live-Call mit
        frischen Werten; beide Caches werden befüllt."""
        client, _ = _make_tws_app(store, monkeypatch)
        with client:
            service = client.app.state.instruments_service
            fresh = _detail().model_copy(
                update={"trading_hours": "20260725:0400-20260725:2000"}
            )

            async def _fake_live(self: Any, contract: Any) -> list[Any]:
                return [_stub_details(fresh)]

            monkeypatch.setattr(
                TWSInstrumentsService,
                "_req_contract_details",
                _fake_live,
            )
            response = client.get("/v1/instruments/14079", headers=_auth())
            assert response.status_code == 200
            body = response.json()
            assert body["trading_hours"] == "20260725:0400-20260725:2000"
            # Live-Fetch hat beide Caches (frisch + Stale) befüllt.
            fresh_hit, ok = service.info_cache.get(14079)
            stale_hit, ok2 = service.info_stale_cache.get(14079)
            assert ok and ok2
            assert fresh_hit.trading_hours == "20260725:0400-20260725:2000"
            assert stale_hit.trading_hours == "20260725:0400-20260725:2000"


class TestCpModeUnchanged:
    """cp-Backend: das dokumentierte 503 ``auth_lost``-Verhalten bleibt
    auf dem Detail-Endpunkt erhalten (BRO-0158 darf cp nicht ändern)."""

    def test_cp_session_lost_still_503_auth_lost(
        self,
        store: InMemoryTokenStore,
        cp_gateway_mock: Any,
    ) -> None:
        cp_client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
        try:
            instruments = InstrumentsService(cp_client, ttl_seconds=300.0)
            cp_gateway_mock.auth_lost = True
            lifecycle = AuthLifecycle(
                cp_client,
                tickle_interval_s=10.0,
                reauth_max_retries=1,
                reauth_backoff_s=0.0,
            )
            asyncio.run(lifecycle.tick_once())
            assert lifecycle.status is AuthStatus.AUTH_LOST

            application = create_app(
                store=store, lifecycle=lifecycle, instruments_service=instruments
            )
            with TestClient(application) as test_client:
                response = test_client.get(
                    "/v1/instruments/14079", headers=_auth()
                )
            assert response.status_code == 503
            assert response.headers["retry-after"] == "30"
            assert response.json()["error"]["code"] == "auth_lost"
            asyncio.run(lifecycle.stop())
        finally:
            asyncio.run(cp_client.aclose())
