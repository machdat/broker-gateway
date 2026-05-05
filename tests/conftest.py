"""Pytest-Fixtures fuer broker-gateway.

Single Source of Truth fuer Mock-Antworten ist seit AP-02 #03 das
Verzeichnis ``tests/fixtures/recorded/`` (siehe
``tests/cp_mock/replay.py``). Statische Bodies werden aus
seed/live-Recordings geladen, stateful Endpoints (snapshot, orders,
trades, unsubscribe) generieren weiterhin im Code - die werden in
AP-02 #04 durch live-Recordings ergaenzt.

Die Fixture-API (Felder wie ``auth_lost``, ``slow_response_ms``,
``pacing_violation_after_n``, ``reply_warnings``,
``omit_trade_currency``, ``subscriptions``, ``request_count``) bleibt
unveraendert, damit alle Tests aus AP-01 ohne Anpassung weiterlaufen.
"""
from __future__ import annotations

import pytest
import respx

from tests.cp_mock import ReplayCPGatewayMock


@pytest.fixture(autouse=True)
def _default_stack_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setzt ``BG_STACK_KIND=paper`` und entfernt Auto-Login-Vars als
    Default, damit Tests den globalen ``validate_runtime_config``-Hard-
    Guard nicht versehentlich triggern.

    Tests in ``test_config.py``, die einen anderen Wert oder eine andere
    Konstellation pruefen wollen, setzen die Env-Vars per
    ``monkeypatch.setenv`` selbst — die haben Vorrang vor diesem
    Default, weil pytest die monkeypatch-Reihenfolge so einhaelt.
    """
    monkeypatch.setenv("BG_STACK_KIND", "paper")
    monkeypatch.delenv("BG_PAPER_AUTO_LOGIN", raising=False)
    monkeypatch.delenv("BG_PAPER_USERNAME", raising=False)
    monkeypatch.delenv("BG_PAPER_PASSWORD", raising=False)


@pytest.fixture
def cp_gateway_mock():
    """Liefert ein konfigurierbares CP-Gateway-Mock und haengt es an httpx.

    Standardwert fuer die Base-URL ist ``http://cpgateway:5000/v1/api``,
    der Live-Pfad des IBKR Client Portal Gateway. Tests, die eine andere
    Base-URL benoetigen, instanziieren ``ReplayCPGatewayMock`` selbst und
    registrieren es im aktiven respx-Router.
    """
    mock = ReplayCPGatewayMock("http://cpgateway:5000/v1/api")
    with respx.mock(assert_all_called=False) as router:
        mock.register(router)
        yield mock
