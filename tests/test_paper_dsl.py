"""Unit-Tests fuer tests_paper/_dsl/* (AP-07 K3-K5).

Laufen in der Standard-tests/-Suite (kein Paper-Stack noetig), pruefen
nur die Pure-Python-Logik der DSL-Layer.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tests_paper._dsl.assertions import (
    assert_money_close,
    assert_order_status_in,
    assert_side_valid,
)
from tests_paper._dsl.safety import (
    PaperSafetyError,
    assert_paper_account,
    assert_within_paper_limits,
)


# ---------------------------------------------------------------------------
# safety.assert_paper_account
# ---------------------------------------------------------------------------


def test_assert_paper_account_accepts_du_prefix() -> None:
    assert assert_paper_account("DU1234567") == "DU1234567"
    assert assert_paper_account("  du1234567  ") == "du1234567"


def test_assert_paper_account_rejects_live_account() -> None:
    with pytest.raises(PaperSafetyError, match="DU-Praefix"):
        assert_paper_account("U25235077")


def test_assert_paper_account_rejects_empty() -> None:
    with pytest.raises(PaperSafetyError, match="nicht gesetzt"):
        assert_paper_account(None)
    with pytest.raises(PaperSafetyError, match="nicht gesetzt"):
        assert_paper_account("")


# ---------------------------------------------------------------------------
# safety.assert_within_paper_limits
# ---------------------------------------------------------------------------


def test_within_limits_default_ok() -> None:
    # Default: 1.00 USD pro Order, 10.00 USD kumulativ.
    assert_within_paper_limits(
        notional_usd=Decimal("0.50"),
        cumulative_notional_usd=Decimal("0.50"),
    )


def test_within_limits_per_order_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BG_PAPER_MAX_NOTIONAL_USD", raising=False)
    with pytest.raises(PaperSafetyError, match="Per-Order"):
        assert_within_paper_limits(
            notional_usd=Decimal("5.00"),
            cumulative_notional_usd=Decimal("5.00"),
        )


def test_within_limits_total_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BG_PAPER_MAX_NOTIONAL_TOTAL_USD", raising=False)
    with pytest.raises(PaperSafetyError, match="Test-Limit"):
        assert_within_paper_limits(
            notional_usd=Decimal("0.50"),
            cumulative_notional_usd=Decimal("100.00"),
        )


def test_within_limits_env_override_lifts_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BG_PAPER_MAX_NOTIONAL_USD", "100")
    monkeypatch.setenv("BG_PAPER_MAX_NOTIONAL_TOTAL_USD", "1000")
    # Keine Exception erwartet.
    assert_within_paper_limits(
        notional_usd=Decimal("50"),
        cumulative_notional_usd=Decimal("500"),
    )


# ---------------------------------------------------------------------------
# assertions.assert_money_close
# ---------------------------------------------------------------------------


def test_money_close_within_tolerance() -> None:
    assert_money_close(Decimal("100.005"), Decimal("100.00"))
    assert_money_close("100.01", 100)


def test_money_close_outside_tolerance_fails() -> None:
    with pytest.raises(AssertionError, match="Delta"):
        assert_money_close(Decimal("100.50"), Decimal("100.00"))


def test_money_close_none_fails() -> None:
    with pytest.raises(AssertionError, match="None"):
        assert_money_close(None, "100.00")


# ---------------------------------------------------------------------------
# assertions.assert_order_status_in
# ---------------------------------------------------------------------------


def test_order_status_in_accepts_match() -> None:
    assert_order_status_in("filled", ["FILLED", "cancelled"])


def test_order_status_in_rejects_mismatch() -> None:
    with pytest.raises(AssertionError, match="rejected"):
        assert_order_status_in("rejected", ["filled", "cancelled"])


def test_order_status_in_rejects_none() -> None:
    with pytest.raises(AssertionError, match="None"):
        assert_order_status_in(None, ["filled"])


# ---------------------------------------------------------------------------
# assertions.assert_side_valid
# ---------------------------------------------------------------------------


def test_side_valid_buy_sell() -> None:
    assert_side_valid("BUY")
    assert_side_valid("sell")


def test_side_valid_rejects_other() -> None:
    with pytest.raises(AssertionError, match="BUY"):
        assert_side_valid("HOLD")
    with pytest.raises(AssertionError, match="leer"):
        assert_side_valid(None)


# ---------------------------------------------------------------------------
# actions.PaperActions Konstruktor + Notional-Tracking (Smoke)
# ---------------------------------------------------------------------------


def test_paper_actions_rejects_live_account_at_init() -> None:
    from tests_paper._dsl.actions import PaperActions  # noqa: PLC0415

    class _DummyClient:
        async def post(self, *a, **k):  # pragma: no cover
            return None

    with pytest.raises(PaperSafetyError, match="DU-Praefix"):
        PaperActions(client=_DummyClient(), account_id="U25235077")  # type: ignore[arg-type]


def test_paper_actions_accepts_du_account_at_init() -> None:
    from tests_paper._dsl.actions import PaperActions  # noqa: PLC0415

    class _DummyClient:
        pass

    actions = PaperActions(
        client=_DummyClient(), account_id="DU1234567"  # type: ignore[arg-type]
    )
    assert actions.account_id == "DU1234567"
    assert actions.cumulative_notional_usd == Decimal("0")
