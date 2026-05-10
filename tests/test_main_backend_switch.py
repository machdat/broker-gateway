"""Tests fuer broker_gateway.main.create_app + BG_BACKEND-Switch (Karte
33cb35b1 Phase 4).

Pruefen, dass:

- BG_BACKEND=cp (Default) baut eine ``cp.AuthLifecycle``-Instanz und
  haengt sie unter ``app.state.cp_lifecycle``.
- BG_BACKEND=tws baut einen ``TWSLifecycleCpAdapter``, der einen
  ``TWSLifecycle`` wrappt. ``app.state.cp_lifecycle`` ist der Adapter.
- /v1/internal/health rendert in beiden Modi dasselbe Response-Schema
  (Field-Set + Typen), nur die Werte unterscheiden sich.
- Ungueltige BG_BACKEND-Werte fallen mit Warning auf cp zurueck.

Strategie: Wir verwenden den ``lifecycle``-Override-Parameter von
``create_app``, um den BG_BACKEND-Pfad gezielt zu simulieren - das
umgeht die echte ib_async-Connection. Der Switch selbst wird durch
einen separaten Test gegen ``backend_kind()`` und durch einen
End-to-End-Test mit gemocktem ``TWSClient`` verifiziert.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.auth.store import InMemoryTokenStore
from broker_gateway.auth_status import AuthStatus
from broker_gateway.config import backend_kind
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app
from broker_gateway.tws.lifecycle import TWSLifecycle, TWSLifecycleCpAdapter


_ADMIN_VALUE = "backend-switch-admin-token-aaaaaaaaaaaaaaa"


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(
        Token(
            value=_ADMIN_VALUE,
            caller_id="bootstrap-admin",
            scopes=[SCOPE_ADMIN_ALL],
        )
    )
    return s


@pytest.fixture(autouse=True)
def stack_kind_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_runtime_config braucht BG_STACK_KIND."""
    monkeypatch.setenv("BG_STACK_KIND", "paper")


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_ADMIN_VALUE}"}


# --------------------------------------------------------------------------
# backend_kind() ENV-Logik
# --------------------------------------------------------------------------


class TestBackendKind:
    def test_default_is_cp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BG_BACKEND", raising=False)
        assert backend_kind() == "cp"

    def test_explicit_cp(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BG_BACKEND", "cp")
        assert backend_kind() == "cp"

    def test_explicit_tws(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BG_BACKEND", "tws")
        assert backend_kind() == "tws"

    def test_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BG_BACKEND", "TWS")
        assert backend_kind() == "tws"

    def test_invalid_falls_back_to_cp_with_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("BG_BACKEND", "garbage")
        with caplog.at_level("WARNING"):
            result = backend_kind()
        assert result == "cp"
        assert any("garbage" in m for m in caplog.messages)


# --------------------------------------------------------------------------
# create_app + Lifecycle-Wahl
# --------------------------------------------------------------------------


def _ensure_admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_BOOTSTRAP_ADMIN_TOKEN", _ADMIN_VALUE)


class TestCpBackend:
    """Default-Pfad: BG_BACKEND nicht gesetzt → AuthLifecycle."""

    def test_internal_health_uses_cp_lifecycle(
        self,
        store: InMemoryTokenStore,
        cp_gateway_mock: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("BG_BACKEND", raising=False)
        _ensure_admin_env(monkeypatch)
        cp_client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
        lifecycle = AuthLifecycle(
            cp_client,
            tickle_interval_s=10.0,
            reauth_max_retries=1,
            reauth_backoff_s=0.0,
        )
        app = create_app(store=store, lifecycle=lifecycle)
        with TestClient(app) as client:
            assert isinstance(app.state.cp_lifecycle, AuthLifecycle)
            response = client.get(
                "/v1/internal/health", headers=_auth_headers()
            )
        assert response.status_code == 200
        body = response.json()
        # Schema-Gemeinsamkeit: auth_status + auth_status_consumer
        assert "auth_status" in body
        assert "auth_status_consumer" in body
        assert body["auth_status_consumer"] in {"ok", "down", "lost"}


class TestTwsBackend:
    """BG_BACKEND=tws → TWSLifecycleCpAdapter."""

    def test_internal_health_uses_tws_lifecycle_adapter(
        self,
        store: InMemoryTokenStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BG_BACKEND", "tws")
        _ensure_admin_env(monkeypatch)
        # Wir injizieren einen vorgebauten TWSLifecycleCpAdapter ueber
        # den lifecycle-Parameter, damit der Lifespan keinen echten
        # TWSClient.connect() macht. Der Switch im owned_lifecycle-
        # Branch wird separat in TestSwitchOwnedLifecycle geprueft.
        ib_mock = MagicMock()
        ib_mock.isConnected = MagicMock(return_value=True)
        ib_mock.connectAsync = MagicMock()
        ib_mock.disconnect = MagicMock()
        from broker_gateway.tws.client import TWSClient

        client = TWSClient(ib=ib_mock, paper=True)
        lifecycle = TWSLifecycle(client, heartbeat_interval_s=10.0)
        adapter = TWSLifecycleCpAdapter(lifecycle)
        # _is_ready() faellt auf is_connected zurueck (Mock hat kein
        # _ib.client.isReady), also OK bei isConnected=True.
        app = create_app(store=store, lifecycle=adapter)
        with TestClient(app) as test_client:
            assert isinstance(
                app.state.cp_lifecycle, TWSLifecycleCpAdapter
            )
            response = test_client.get(
                "/v1/internal/health", headers=_auth_headers()
            )
        assert response.status_code == 200
        body = response.json()
        assert body["auth_status"] in {
            "ok",
            "tws_down",
            "session_lost",
        }
        assert body["auth_status_consumer"] in {"ok", "down", "lost"}
        # cp-spezifische Felder sind None oder Default
        assert body["last_reauth_at"] is None
        assert body["last_sso_validate_at"] is None
        assert body["iserver_bridge_ok"] is None


class TestSchemaParity:
    """Karten-Constraint: /v1/internal/health-Schema bei BG_BACKEND=cp
    und =tws muss strukturell dieselben Felder haben."""

    def test_field_set_is_identical(
        self,
        store: InMemoryTokenStore,
        cp_gateway_mock: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _ensure_admin_env(monkeypatch)

        # CP-Variante
        monkeypatch.setenv("BG_BACKEND", "cp")
        cp_client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
        cp_lifecycle = AuthLifecycle(
            cp_client,
            tickle_interval_s=10.0,
            reauth_max_retries=1,
            reauth_backoff_s=0.0,
        )
        cp_app = create_app(store=store, lifecycle=cp_lifecycle)
        with TestClient(cp_app) as cp_test:
            cp_body = cp_test.get(
                "/v1/internal/health", headers=_auth_headers()
            ).json()
        cp_fields = set(cp_body.keys())

        # TWS-Variante
        monkeypatch.setenv("BG_BACKEND", "tws")
        ib_mock = MagicMock()
        ib_mock.isConnected = MagicMock(return_value=True)
        ib_mock.connectAsync = MagicMock()
        ib_mock.disconnect = MagicMock()
        from broker_gateway.tws.client import TWSClient

        tws_inner_client = TWSClient(ib=ib_mock, paper=True)
        tws_lifecycle = TWSLifecycle(
            tws_inner_client, heartbeat_interval_s=10.0
        )
        tws_adapter = TWSLifecycleCpAdapter(tws_lifecycle)
        store_tws = InMemoryTokenStore()
        store_tws.put(
            Token(
                value=_ADMIN_VALUE,
                caller_id="bootstrap-admin",
                scopes=[SCOPE_ADMIN_ALL],
            )
        )
        tws_app = create_app(store=store_tws, lifecycle=tws_adapter)
        with TestClient(tws_app) as tws_test:
            tws_body = tws_test.get(
                "/v1/internal/health", headers=_auth_headers()
            ).json()
        tws_fields = set(tws_body.keys())

        assert cp_fields == tws_fields, (
            f"Schema-Drift: nur in cp={cp_fields - tws_fields}, "
            f"nur in tws={tws_fields - cp_fields}"
        )


class TestSwitchOwnedLifecycle:
    """End-to-End-Pruefung des owned-lifecycle-Branches in main.py.

    Hier soll create_app OHNE expliziten lifecycle-Parameter laufen,
    damit der BG_BACKEND-Switch in der Lifespan-Funktion wirkt. Der
    echte TWSClient wuerde gegen 4002 connecten - das mocken wir.
    """

    def test_owned_lifecycle_switches_on_bg_backend(
        self,
        store: InMemoryTokenStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BG_BACKEND", "tws")
        _ensure_admin_env(monkeypatch)

        # Patche TWSClient.connect zu einem No-op, damit der Lifespan
        # nicht zum echten IB Gateway zu connecten versucht.
        connect_calls = {"n": 0}

        async def _fake_connect(self: Any) -> None:
            connect_calls["n"] += 1
            self._client_id = 100

        async def _fake_disconnect(self: Any) -> None:
            pass

        def _fake_is_connected(self: Any) -> bool:
            return connect_calls["n"] > 0

        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.connect", _fake_connect
        )
        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.disconnect", _fake_disconnect
        )
        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.is_connected",
            _fake_is_connected,
        )

        app = create_app(store=store)
        with TestClient(app):
            # cp_lifecycle ist der Adapter, app.state.tws_client ist
            # der gleiche Client den TWSLifecycle verwaltet.
            assert isinstance(
                app.state.cp_lifecycle, TWSLifecycleCpAdapter
            )
            assert hasattr(app.state, "tws_client")
        assert connect_calls["n"] >= 1

    def test_owned_lifecycle_uses_tws_portfolio_service(
        self,
        store: InMemoryTokenStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AP `2a203c58-...` Phase 1: BG_BACKEND=tws muss
        TWSPortfolioService unter app.state.portfolio_service haengen,
        nicht den cp.PortfolioService."""
        from broker_gateway.tws.portfolio import TWSPortfolioService

        monkeypatch.setenv("BG_BACKEND", "tws")
        _ensure_admin_env(monkeypatch)

        connect_calls = {"n": 0}

        async def _fake_connect(self: Any) -> None:
            connect_calls["n"] += 1
            self._client_id = 100

        async def _fake_disconnect(self: Any) -> None:
            pass

        def _fake_is_connected(self: Any) -> bool:
            return connect_calls["n"] > 0

        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.connect", _fake_connect
        )
        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.disconnect", _fake_disconnect
        )
        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.is_connected",
            _fake_is_connected,
        )

        app = create_app(store=store)
        with TestClient(app):
            assert isinstance(
                app.state.portfolio_service, TWSPortfolioService
            )

    def test_owned_lifecycle_uses_tws_instruments_service(
        self,
        store: InMemoryTokenStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AP `2a203c58-...` Phase 2: BG_BACKEND=tws muss
        TWSInstrumentsService unter app.state.instruments_service haengen."""
        from broker_gateway.tws.instruments import TWSInstrumentsService

        monkeypatch.setenv("BG_BACKEND", "tws")
        _ensure_admin_env(monkeypatch)

        connect_calls = {"n": 0}

        async def _fake_connect(self: Any) -> None:
            connect_calls["n"] += 1
            self._client_id = 100

        async def _fake_disconnect(self: Any) -> None:
            pass

        def _fake_is_connected(self: Any) -> bool:
            return connect_calls["n"] > 0

        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.connect", _fake_connect
        )
        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.disconnect", _fake_disconnect
        )
        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.is_connected",
            _fake_is_connected,
        )

        app = create_app(store=store)
        with TestClient(app):
            assert isinstance(
                app.state.instruments_service, TWSInstrumentsService
            )

    def test_cp_backend_uses_cp_instruments_service(
        self,
        store: InMemoryTokenStore,
        cp_gateway_mock: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Schwesterfall: BG_BACKEND=cp behaelt cp.InstrumentsService."""
        from broker_gateway.cp.instruments import InstrumentsService

        monkeypatch.setenv("BG_BACKEND", "cp")
        _ensure_admin_env(monkeypatch)
        cp_client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
        lifecycle = AuthLifecycle(
            cp_client,
            tickle_interval_s=10.0,
            reauth_max_retries=1,
            reauth_backoff_s=0.0,
        )
        app = create_app(store=store, lifecycle=lifecycle)
        with TestClient(app):
            assert isinstance(
                app.state.instruments_service, InstrumentsService
            )

    def test_cp_backend_uses_cp_portfolio_service(
        self,
        store: InMemoryTokenStore,
        cp_gateway_mock: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Schwesterfall: BG_BACKEND=cp behaelt cp.PortfolioService."""
        from broker_gateway.cp.portfolio import PortfolioService

        monkeypatch.setenv("BG_BACKEND", "cp")
        _ensure_admin_env(monkeypatch)
        cp_client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
        lifecycle = AuthLifecycle(
            cp_client,
            tickle_interval_s=10.0,
            reauth_max_retries=1,
            reauth_backoff_s=0.0,
        )
        app = create_app(store=store, lifecycle=lifecycle)
        with TestClient(app):
            assert isinstance(
                app.state.portfolio_service, PortfolioService
            )

    def test_owned_lifecycle_uses_tws_quotes_service(
        self,
        store: InMemoryTokenStore,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AP `2a203c58-...` Phase 3: BG_BACKEND=tws muss
        TWSQuotesService unter app.state.quotes_service haengen, und
        derselbe Service muss auch als subscription_manager wirken
        (Duck-Typing - implementiert subscribe(...))."""
        from broker_gateway.tws.quotes import TWSQuotesService

        monkeypatch.setenv("BG_BACKEND", "tws")
        _ensure_admin_env(monkeypatch)

        connect_calls = {"n": 0}

        async def _fake_connect(self: Any) -> None:
            connect_calls["n"] += 1
            self._client_id = 100

        async def _fake_disconnect(self: Any) -> None:
            pass

        def _fake_is_connected(self: Any) -> bool:
            return connect_calls["n"] > 0

        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.connect", _fake_connect
        )
        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.disconnect", _fake_disconnect
        )
        monkeypatch.setattr(
            "broker_gateway.tws.client.TWSClient.is_connected",
            _fake_is_connected,
        )

        app = create_app(store=store)
        with TestClient(app):
            assert isinstance(
                app.state.quotes_service, TWSQuotesService
            )
            # Der gleiche Service wird auch als Stream-Quelle hinterlegt.
            assert app.state.subscription_manager is app.state.quotes_service

    def test_cp_backend_uses_cp_quotes_service(
        self,
        store: InMemoryTokenStore,
        cp_gateway_mock: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Schwesterfall: BG_BACKEND=cp behaelt cp.QuotesService und
        einen separaten SubscriptionManager (Polling-Pfad)."""
        from broker_gateway.cp.quotes import QuotesService
        from broker_gateway.streams.manager import SubscriptionManager

        monkeypatch.setenv("BG_BACKEND", "cp")
        _ensure_admin_env(monkeypatch)
        cp_client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
        lifecycle = AuthLifecycle(
            cp_client,
            tickle_interval_s=10.0,
            reauth_max_retries=1,
            reauth_backoff_s=0.0,
        )
        app = create_app(store=store, lifecycle=lifecycle)
        with TestClient(app):
            assert isinstance(app.state.quotes_service, QuotesService)
            assert isinstance(
                app.state.subscription_manager, SubscriptionManager
            )
