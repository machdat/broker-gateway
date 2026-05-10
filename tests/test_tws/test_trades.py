"""Tests fuer broker_gateway.tws.trades (Karte 064fa82d, Phase 4).

Coverage-Ziel: >=90% fuer src/broker_gateway/tws/trades.py.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from broker_gateway.tws.trades import (
    Trade,
    TradesAggregate,
    TWSTradesService,
    _coerce_datetime,
    _decimal_str,
    _fill_to_trade,
    _is_finite_number,
    _within_window,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _make_contract(
    *, conid: int = 265598, symbol: str = "AAPL", currency: str = "USD"
) -> SimpleNamespace:
    return SimpleNamespace(conId=conid, symbol=symbol, currency=currency)


def _make_execution(
    *,
    exec_id: str = "exec-1",
    perm_id: int = 999,
    order_id: int = 10,
    side: str = "BOT",
    shares: float = 10.0,
    price: float = 200.0,
    acct: str = "U25235077",
    time_str: str = "20260510-12:30:45",
) -> SimpleNamespace:
    return SimpleNamespace(
        execId=exec_id,
        permId=perm_id,
        orderId=order_id,
        side=side,
        shares=shares,
        price=price,
        acctNumber=acct,
        time=time_str,
        cumQty=shares,
        avgPrice=price,
    )


def _make_commission_report(
    *, commission: float = 1.0, currency: str | None = "USD"
) -> SimpleNamespace:
    return SimpleNamespace(commission=commission, currency=currency)


def _make_fill(
    *,
    contract: SimpleNamespace | None = None,
    execution: SimpleNamespace | None = None,
    report: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        contract=contract if contract is not None else _make_contract(),
        execution=execution if execution is not None else _make_execution(),
        commissionReport=report if report is not None else _make_commission_report(),
    )


def _make_client(
    *,
    fills: list[SimpleNamespace] | None = None,
    req_executions: list[SimpleNamespace] | None = None,
    req_executions_side_effect: Any = None,
    fills_side_effect: Any = None,
    has_req_executions: bool = True,
) -> MagicMock:
    fill_list = list(fills or [])
    ib_kwargs: dict[str, Any] = {
        "fills": MagicMock(side_effect=fills_side_effect, return_value=fill_list)
        if fills_side_effect
        else MagicMock(return_value=fill_list),
    }
    if has_req_executions:
        if req_executions_side_effect is not None:
            ib_kwargs["reqExecutionsAsync"] = AsyncMock(side_effect=req_executions_side_effect)
        else:
            ib_kwargs["reqExecutionsAsync"] = AsyncMock(
                return_value=list(req_executions or [])
            )
    ib = SimpleNamespace(**ib_kwargs)
    client = MagicMock()
    client._ib = ib
    return client


# --------------------------------------------------------------------------
# list_trades
# --------------------------------------------------------------------------


class TestListTrades:
    async def test_returns_trades_in_window(self) -> None:
        fills = [
            _make_fill(execution=_make_execution(time_str="20260510-12:30:45")),
            _make_fill(execution=_make_execution(exec_id="exec-2", time_str="20260509-08:00:00")),
        ]
        client = _make_client(req_executions=fills)
        service = TWSTradesService(client)
        trades = await service.list_trades(
            period_from=date(2026, 5, 9), period_to=date(2026, 5, 10)
        )
        assert len(trades) == 2

    async def test_filters_outside_window(self) -> None:
        fills = [
            _make_fill(execution=_make_execution(time_str="20260510-12:30:45")),
            _make_fill(execution=_make_execution(exec_id="exec-old", time_str="20260101-08:00:00")),
        ]
        client = _make_client(req_executions=fills)
        service = TWSTradesService(client)
        trades = await service.list_trades(
            period_from=date(2026, 5, 9), period_to=date(2026, 5, 10)
        )
        assert len(trades) == 1
        assert trades[0].execution_id == "exec-1"

    async def test_filters_by_account(self) -> None:
        fills = [
            _make_fill(execution=_make_execution(acct="U25235077")),
            _make_fill(execution=_make_execution(exec_id="exec-2", acct="DUP799747")),
        ]
        client = _make_client(req_executions=fills)
        service = TWSTradesService(client)
        trades = await service.list_trades(
            period_from=date(2026, 5, 1),
            period_to=date(2026, 5, 31),
            account_id="U25235077",
        )
        assert len(trades) == 1
        assert trades[0].account_id == "U25235077"

    async def test_invalid_window_raises_400(self) -> None:
        client = _make_client(req_executions=[])
        service = TWSTradesService(client)
        with pytest.raises(HTTPException) as exc_info:
            await service.list_trades(
                period_from=date(2026, 5, 10), period_to=date(2026, 5, 1)
            )
        assert exc_info.value.status_code == 400

    async def test_falls_back_to_fills_when_req_executions_fails(self) -> None:
        fallback_fills = [_make_fill()]
        client = _make_client(
            fills=fallback_fills,
            req_executions_side_effect=RuntimeError("ibkr down"),
        )
        service = TWSTradesService(client)
        trades = await service.list_trades(
            period_from=date(2026, 5, 1), period_to=date(2026, 5, 31)
        )
        assert len(trades) == 1

    async def test_uses_fills_when_no_req_executions_method(self) -> None:
        fallback_fills = [_make_fill()]
        client = _make_client(
            fills=fallback_fills,
            has_req_executions=False,
        )
        service = TWSTradesService(client)
        trades = await service.list_trades(
            period_from=date(2026, 5, 1), period_to=date(2026, 5, 31)
        )
        assert len(trades) == 1

    async def test_handles_fills_exception(self) -> None:
        client = _make_client(
            fills_side_effect=RuntimeError("fills broken"),
            req_executions_side_effect=RuntimeError("ibkr down"),
        )
        service = TWSTradesService(client)
        trades = await service.list_trades(
            period_from=date(2026, 5, 1), period_to=date(2026, 5, 31)
        )
        assert trades == []

    async def test_handles_empty_account_in_filter(self) -> None:
        fills = [_make_fill(execution=_make_execution())]
        client = _make_client(req_executions=fills)
        service = TWSTradesService(client)
        # Kein account_id-Filter → alle Trades zaehlen
        trades = await service.list_trades(
            period_from=date(2026, 5, 1), period_to=date(2026, 5, 31)
        )
        assert len(trades) == 1


# --------------------------------------------------------------------------
# commissions_mtd
# --------------------------------------------------------------------------


class TestCommissionsMtd:
    async def test_aggregates_commissions(self) -> None:
        fills = [
            _make_fill(report=_make_commission_report(commission=0.5)),
            _make_fill(
                execution=_make_execution(exec_id="exec-2", time_str="20260505-08:00:00"),
                report=_make_commission_report(commission=0.7),
            ),
        ]
        client = _make_client(req_executions=fills)
        service = TWSTradesService(client)
        agg = await service.commissions_mtd(
            account_id="U25235077", today=date(2026, 5, 10)
        )
        assert isinstance(agg, TradesAggregate)
        assert agg.metric == "commissions_mtd"
        assert Decimal(agg.value.value) == Decimal("1.2")
        assert agg.value.currency == "USD"
        assert agg.trade_count == 2
        assert agg.currency_assumption is None

    async def test_falls_back_to_usd_when_empty(self) -> None:
        client = _make_client(req_executions=[])
        service = TWSTradesService(client)
        agg = await service.commissions_mtd(today=date(2026, 5, 10))
        assert agg.value.currency == "USD"
        assert Decimal(agg.value.value) == Decimal("0")
        assert agg.trade_count == 0

    async def test_skips_other_currency(self) -> None:
        fills = [
            _make_fill(
                contract=_make_contract(currency="USD"),
                report=_make_commission_report(commission=0.5, currency="USD"),
            ),
            _make_fill(
                contract=_make_contract(currency="EUR"),
                execution=_make_execution(exec_id="exec-2", time_str="20260505-08:00:00"),
                report=_make_commission_report(commission=2.0, currency="EUR"),
            ),
        ]
        client = _make_client(req_executions=fills)
        service = TWSTradesService(client)
        agg = await service.commissions_mtd(today=date(2026, 5, 10))
        # Erste Trade in der Liste (USD) gewinnt; EUR-Trade wird nicht summiert.
        assert agg.value.currency == "USD"
        assert Decimal(agg.value.value) == Decimal("0.5")
        assert agg.trade_count == 2  # alle Trades zaehlen, nur Summe filtert

    async def test_marks_currency_assumption_when_unknown(self) -> None:
        fills = [
            _make_fill(
                contract=SimpleNamespace(conId=1, symbol="X", currency=None),
                report=SimpleNamespace(commission=1.0, currency=None),
            ),
        ]
        client = _make_client(req_executions=fills)
        service = TWSTradesService(client)
        agg = await service.commissions_mtd(today=date(2026, 5, 10))
        assert agg.currency_assumption == "USD"


# --------------------------------------------------------------------------
# _fill_to_trade
# --------------------------------------------------------------------------


class TestFillToTrade:
    def test_full_fill_maps_all_fields(self) -> None:
        fill = _make_fill()
        trade = _fill_to_trade(fill)
        assert trade is not None
        assert trade.execution_id == "exec-1"
        assert trade.order_id == "999"
        assert trade.account_id == "U25235077"
        assert trade.conid == 265598
        assert trade.symbol == "AAPL"
        assert trade.side == "BOT"
        assert trade.quantity is not None and Decimal(trade.quantity) == Decimal("10")
        assert trade.price is not None and Decimal(trade.price.value) == Decimal("200")
        assert trade.price.currency == "USD"
        assert trade.net_amount is not None and Decimal(trade.net_amount.value) == Decimal("2000")
        assert trade.commission is not None and Decimal(trade.commission.value) == Decimal("1")
        assert trade.executed_at is not None
        assert trade.executed_at.year == 2026

    def test_returns_none_when_execution_missing(self) -> None:
        fill = SimpleNamespace(execution=None, contract=None, commissionReport=None)
        assert _fill_to_trade(fill) is None

    def test_uses_perm_id_when_present(self) -> None:
        fill = _make_fill(execution=_make_execution(perm_id=12345, order_id=7))
        trade = _fill_to_trade(fill)
        assert trade.order_id == "12345"

    def test_falls_back_to_order_id_when_perm_id_zero(self) -> None:
        fill = _make_fill(execution=_make_execution(perm_id=0, order_id=42))
        trade = _fill_to_trade(fill)
        assert trade.order_id == "42"

    def test_marks_currency_assumed(self) -> None:
        fill = _make_fill(
            contract=SimpleNamespace(conId=1, symbol="X", currency=None),
            report=SimpleNamespace(commission=0.5, currency=None),
        )
        trade = _fill_to_trade(fill)
        assert trade.currency_assumed is True

    def test_handles_missing_commission_report(self) -> None:
        fill = SimpleNamespace(
            contract=_make_contract(),
            execution=_make_execution(),
            commissionReport=None,
        )
        trade = _fill_to_trade(fill)
        assert trade is not None
        assert trade.commission is None

    def test_handles_nan_price(self) -> None:
        fill = _make_fill(execution=_make_execution(price=float("nan")))
        trade = _fill_to_trade(fill)
        assert trade is not None
        assert trade.price is None
        assert trade.net_amount is None


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


class TestPureHelpers:
    def test_within_window_inside(self) -> None:
        ts = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        assert _within_window(ts, date(2026, 5, 1), date(2026, 5, 31)) is True

    def test_within_window_before(self) -> None:
        ts = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)
        assert _within_window(ts, date(2026, 5, 1), date(2026, 5, 31)) is False

    def test_within_window_none_passes(self) -> None:
        assert _within_window(None, date(2026, 5, 1), date(2026, 5, 31)) is True

    def test_coerce_datetime_ibkr_format(self) -> None:
        result = _coerce_datetime("20260510-12:30:45")
        assert result is not None
        assert result.year == 2026 and result.month == 5

    def test_coerce_datetime_iso_with_timezone(self) -> None:
        result = _coerce_datetime("2026-05-10T12:30:45+00:00")
        assert result is not None
        assert result.tzinfo is not None

    def test_coerce_datetime_returns_none_for_unknown(self) -> None:
        assert _coerce_datetime("nonsense") is None

    def test_coerce_datetime_passes_through_aware_datetime(self) -> None:
        ts = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        assert _coerce_datetime(ts) == ts

    def test_coerce_datetime_makes_naive_utc(self) -> None:
        ts = datetime(2026, 5, 10, 12, 0, 0)
        result = _coerce_datetime(ts)
        assert result is not None and result.tzinfo == timezone.utc

    def test_coerce_datetime_none_returns_none(self) -> None:
        assert _coerce_datetime(None) is None

    def test_decimal_str_int(self) -> None:
        assert _decimal_str(5) == "5"

    def test_decimal_str_none(self) -> None:
        assert _decimal_str(None) is None

    def test_decimal_str_nan(self) -> None:
        assert _decimal_str(float("nan")) is None

    def test_is_finite_number_true(self) -> None:
        assert _is_finite_number(3.14)

    def test_is_finite_number_false_for_bool(self) -> None:
        assert not _is_finite_number(False)

    def test_is_finite_number_decimal(self) -> None:
        assert _is_finite_number(Decimal("3.14"))
