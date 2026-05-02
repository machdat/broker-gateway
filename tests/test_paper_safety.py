"""Unit-Tests fuer tests_paper/_dsl/safety.py (AP-07 K3).

Laufen in der Default-tests/-Suite, kein Paper-Stack noetig.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests_paper._dsl.safety import (
    PaperSafetyError,
    assert_paper_account,
    kill_switch_active,
    max_notional_per_order,
    max_open_orders,
)


# ---------------------------------------------------------------------------
# assert_paper_account
# ---------------------------------------------------------------------------


def test_assert_paper_account_du_prefix_passes() -> None:
    assert assert_paper_account("DU1234567") == "DU1234567"


def test_assert_paper_account_live_id_rejected() -> None:
    with pytest.raises(PaperSafetyError):
        assert_paper_account("U25235077")


def test_assert_paper_account_whitelist_env_unlocks_non_du(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BG_PAPER_ACCOUNT_WHITELIST", "U99999999, X12345")
    assert assert_paper_account("U99999999") == "U99999999"


def test_assert_paper_account_whitelist_strict_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BG_PAPER_ACCOUNT_WHITELIST", "U99999999")
    with pytest.raises(PaperSafetyError):
        assert_paper_account("U88888888")


# ---------------------------------------------------------------------------
# max_notional_per_order
# ---------------------------------------------------------------------------


def test_max_notional_default_passes_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BG_PAPER_MAX_NOTIONAL_PER_ORDER", raising=False)
    # Default 500 USD: 100 * 5 = 500 ist gleich, 100 * 4 = 400 ist drunter.
    max_notional_per_order(100, 4)
    max_notional_per_order(100, 5)


def test_max_notional_default_blocks_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BG_PAPER_MAX_NOTIONAL_PER_ORDER", raising=False)
    with pytest.raises(PaperSafetyError, match="Per-Order"):
        max_notional_per_order(100, 6)


def test_max_notional_env_override_lifts_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BG_PAPER_MAX_NOTIONAL_PER_ORDER", "10000")
    max_notional_per_order(Decimal("100"), Decimal("50"))


# ---------------------------------------------------------------------------
# max_open_orders
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body


class _FakeClient:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.last_path: str | None = None
        self.last_params: dict | None = None

    async def get(self, path: str, *, params: dict | None = None) -> _FakeResponse:
        self.last_path = path
        self.last_params = params
        return self._response


async def test_max_open_orders_passes_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BG_PAPER_MAX_OPEN_ORDERS", raising=False)
    client = _FakeClient(_FakeResponse(200, {"orders": [{}, {}]}))
    await max_open_orders(client, "DU1234567")
    assert client.last_path == "/v1/orders"


async def test_max_open_orders_blocks_above_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BG_PAPER_MAX_OPEN_ORDERS", raising=False)
    client = _FakeClient(_FakeResponse(200, [{}] * 10))
    with pytest.raises(PaperSafetyError, match="offene Orders"):
        await max_open_orders(client, "DU1234567")


async def test_max_open_orders_silent_on_non_200() -> None:
    client = _FakeClient(_FakeResponse(403, {"error": "no scope"}))
    # Defensiv: kein Throw, weil das Diagnose-Mittel nicht greift.
    await max_open_orders(client, "DU1234567")


# ---------------------------------------------------------------------------
# kill_switch_active
# ---------------------------------------------------------------------------


def test_kill_switch_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BG_PAPER_TESTS_DISABLED", raising=False)
    assert kill_switch_active() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_kill_switch_truthy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BG_PAPER_TESTS_DISABLED", value)
    assert kill_switch_active() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_kill_switch_falsy_values(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("BG_PAPER_TESTS_DISABLED", value)
    assert kill_switch_active() is False
