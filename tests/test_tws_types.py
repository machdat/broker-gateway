"""Tests fuer broker_gateway.tws.types (Karte 441b53db).

Pydantic-Modelle und ihre ``from_*``-Mapping-Funktionen.
"""
from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from ib_async import AccountValue, BarData

from broker_gateway.tws.types import (
    AccountField,
    Bar,
    Position,
    Snapshot,
    Tick,
    _decimal_or_none,
    _to_utc,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

class TestDecimalOrNone:
    def test_none_returns_none(self) -> None:
        assert _decimal_or_none(None) is None

    def test_nan_returns_none(self) -> None:
        assert _decimal_or_none(float("nan")) is None

    def test_int_returns_decimal(self) -> None:
        assert _decimal_or_none(42) == Decimal("42")

    def test_float_returns_decimal_via_str(self) -> None:
        # via str() vermeidet float-Praezisionsverlust (1.1 != Decimal('1.1'))
        assert _decimal_or_none(1.5) == Decimal("1.5")

    def test_string_returns_decimal(self) -> None:
        assert _decimal_or_none("3.14") == Decimal("3.14")


class TestToUtc:
    def test_none_returns_none(self) -> None:
        assert _to_utc(None) is None

    def test_naive_datetime_treated_as_utc(self) -> None:
        ts = datetime(2026, 5, 9, 14, 30)
        result = _to_utc(ts)
        assert result is not None
        assert result.tzinfo == UTC
        assert result.hour == 14

    def test_aware_datetime_converted_to_utc(self) -> None:
        plus_two = timezone(timedelta(hours=2))
        ts = datetime(2026, 5, 9, 14, 30, tzinfo=plus_two)
        result = _to_utc(ts)
        assert result is not None
        assert result.tzinfo == UTC
        assert result.hour == 12

    def test_date_becomes_midnight_utc(self) -> None:
        d = date(2026, 5, 9)
        result = _to_utc(d)
        assert result == datetime(2026, 5, 9, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------
# AccountField
# --------------------------------------------------------------------------

class TestAccountField:
    def test_from_account_value_basic(self) -> None:
        av = AccountValue("U25235077", "NetLiquidation", "12345.67", "USD", "")
        field = AccountField.from_account_value(av)
        assert field.account == "U25235077"
        assert field.tag == "NetLiquidation"
        assert field.value == "12345.67"
        assert field.currency == "USD"
        assert field.model_code is None  # leerer String -> None

    def test_from_account_value_with_model_code(self) -> None:
        av = AccountValue("U25235077", "NetLiquidation", "100", "USD", "MODEL_X")
        field = AccountField.from_account_value(av)
        assert field.model_code == "MODEL_X"

    def test_empty_currency_normalized_to_none(self) -> None:
        av = AccountValue("U25235077", "AccountType", "INDIVIDUAL", "", "")
        field = AccountField.from_account_value(av)
        assert field.currency is None

    def test_frozen(self) -> None:
        field = AccountField(account="X", tag="T", value="V")
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            field.value = "Y"  # type: ignore[misc]


# --------------------------------------------------------------------------
# Position
# --------------------------------------------------------------------------

class TestPosition:
    def _portfolio_item(
        self,
        *,
        account: str = "U25235077",
        symbol: str = "AAPL",
        position: float = 100.0,
        avg_cost: float = 150.0,
        market_price: float = 155.0,
        market_value: float = 15500.0,
        unrealized_pnl: float = 500.0,
        realized_pnl: float = 0.0,
    ) -> SimpleNamespace:
        contract = SimpleNamespace(
            conId=265598,
            symbol=symbol,
            secType="STK",
            exchange="SMART",
            currency="USD",
        )
        return SimpleNamespace(
            account=account,
            contract=contract,
            position=position,
            averageCost=avg_cost,
            marketPrice=market_price,
            marketValue=market_value,
            unrealizedPNL=unrealized_pnl,
            realizedPNL=realized_pnl,
        )

    def test_from_portfolio_item_basic(self) -> None:
        item = self._portfolio_item()
        pos = Position.from_portfolio_item(item)
        assert pos.account == "U25235077"
        assert pos.con_id == 265598
        assert pos.symbol == "AAPL"
        assert pos.sec_type == "STK"
        assert pos.exchange == "SMART"
        assert pos.currency == "USD"
        assert pos.position == Decimal("100.0")
        assert pos.average_cost == Decimal("150.0")
        assert pos.market_price == Decimal("155.0")

    def test_from_portfolio_item_nan_market_value_becomes_none(self) -> None:
        item = self._portfolio_item(market_value=float("nan"))
        pos = Position.from_portfolio_item(item)
        assert pos.market_value is None

    def test_from_portfolio_item_empty_exchange_becomes_none(self) -> None:
        item = self._portfolio_item()
        item.contract.exchange = ""
        pos = Position.from_portfolio_item(item)
        assert pos.exchange is None


# --------------------------------------------------------------------------
# Bar
# --------------------------------------------------------------------------

class TestBar:
    def _bar_data(
        self,
        *,
        ts: datetime | date = datetime(2026, 5, 9, 14, 30),
        open_: float = 100.0,
        high: float = 101.0,
        low: float = 99.0,
        close: float = 100.5,
        volume: float = 1234.0,
        average: float = 100.25,
        bar_count: int = 5,
    ) -> BarData:
        bd = BarData()
        bd.date = ts
        bd.open = open_
        bd.high = high
        bd.low = low
        bd.close = close
        bd.volume = volume
        bd.average = average
        bd.barCount = bar_count
        return bd

    def test_from_bar_data_basic(self) -> None:
        bar = Bar.from_bar_data(self._bar_data())
        assert bar.timestamp == datetime(2026, 5, 9, 14, 30, tzinfo=UTC)
        assert bar.open == Decimal("100.0")
        assert bar.close == Decimal("100.5")
        assert bar.volume == Decimal("1234.0")
        assert bar.wap == Decimal("100.25")
        assert bar.bar_count == 5

    def test_from_bar_data_daily_bar_uses_date(self) -> None:
        bar = Bar.from_bar_data(self._bar_data(ts=date(2026, 5, 9)))
        assert bar.timestamp == datetime(2026, 5, 9, 0, 0, tzinfo=UTC)

    def test_from_bar_data_zero_average_becomes_none(self) -> None:
        bar = Bar.from_bar_data(self._bar_data(average=0.0))
        assert bar.wap is None

    def test_from_bar_data_zero_bar_count_becomes_none(self) -> None:
        bar = Bar.from_bar_data(self._bar_data(bar_count=0))
        assert bar.bar_count is None


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------

class TestSnapshot:
    def _ticker(
        self,
        *,
        last: float = 150.5,
        bid: float = 150.4,
        ask: float = 150.6,
        bid_size: float = 100.0,
        ask_size: float = 200.0,
        volume: float = float("nan"),
        market_data_type: int = 3,
        time: datetime | None = None,
    ) -> MagicMock:
        ticker = MagicMock()
        ticker.contract = SimpleNamespace(conId=265598, symbol="AAPL")
        ticker.last = last
        ticker.bid = bid
        ticker.ask = ask
        ticker.bidSize = bid_size
        ticker.askSize = ask_size
        ticker.volume = volume
        ticker.marketDataType = market_data_type
        ticker.time = time or datetime(2026, 5, 9, 14, 30)
        return ticker

    def test_from_ticker_basic(self) -> None:
        snap = Snapshot.from_ticker(self._ticker())
        assert snap.con_id == 265598
        assert snap.symbol == "AAPL"
        assert snap.last == Decimal("150.5")
        assert snap.bid == Decimal("150.4")
        assert snap.ask == Decimal("150.6")
        assert snap.market_data_type == 3
        assert snap.timestamp == datetime(2026, 5, 9, 14, 30, tzinfo=UTC)

    def test_from_ticker_nan_volume_becomes_none(self) -> None:
        snap = Snapshot.from_ticker(self._ticker(volume=float("nan")))
        assert snap.volume is None

    def test_from_ticker_no_contract_uses_zero_and_empty_string(self) -> None:
        ticker = self._ticker()
        ticker.contract = None
        snap = Snapshot.from_ticker(ticker)
        assert snap.con_id == 0
        assert snap.symbol == ""

    def test_from_ticker_no_time_returns_none(self) -> None:
        ticker = self._ticker()
        ticker.time = None
        snap = Snapshot.from_ticker(ticker)
        assert snap.timestamp is None


# --------------------------------------------------------------------------
# Tick
# --------------------------------------------------------------------------

class TestTick:
    def _ticker(self, **overrides: object) -> MagicMock:
        defaults: dict[str, object] = {
            "contract": SimpleNamespace(conId=265598, symbol="AAPL"),
            "last": 150.5,
            "lastSize": 50.0,
            "bid": 150.4,
            "bidSize": 100.0,
            "ask": 150.6,
            "askSize": 200.0,
            "time": datetime(2026, 5, 9, 14, 30),
        }
        defaults.update(overrides)
        ticker = MagicMock()
        for key, value in defaults.items():
            setattr(ticker, key, value)
        return ticker

    def test_from_ticker_default_field_is_last(self) -> None:
        tick = Tick.from_ticker(self._ticker())
        assert tick.field == "last"
        assert tick.value == Decimal("150.5")
        assert tick.size == Decimal("50.0")

    def test_from_ticker_field_bid(self) -> None:
        tick = Tick.from_ticker(self._ticker(), field="bid")
        assert tick.field == "bid"
        assert tick.value == Decimal("150.4")
        assert tick.size == Decimal("100.0")

    def test_from_ticker_field_ask(self) -> None:
        tick = Tick.from_ticker(self._ticker(), field="ask")
        assert tick.value == Decimal("150.6")
        assert tick.size == Decimal("200.0")

    def test_from_ticker_no_time_uses_now_utc(self) -> None:
        before = datetime.now(UTC)
        tick = Tick.from_ticker(self._ticker(time=None))
        after = datetime.now(UTC)
        assert before <= tick.timestamp <= after
        assert tick.timestamp.tzinfo == UTC


# --------------------------------------------------------------------------
# Decimal-Edge-Cases (Disziplin)
# --------------------------------------------------------------------------

def test_decimal_via_str_avoids_float_precision_loss() -> None:
    """Sicherstellen, dass _decimal_or_none(0.1) nicht in Decimal('0.1000000...59')
    landet. Die Library nutzt str(value), das vermeidet float-Repr."""
    assert _decimal_or_none(0.1) == Decimal("0.1")
    assert _decimal_or_none(0.2) == Decimal("0.2")


def test_to_utc_idempotent_for_utc_aware() -> None:
    ts = datetime(2026, 5, 9, 14, 30, tzinfo=UTC)
    assert _to_utc(ts) == ts
    assert math.isclose((_to_utc(ts) - ts).total_seconds(), 0.0)
