"""Tests fuer broker_gateway.tws.contracts (Karte 441b53db)."""
from __future__ import annotations

from broker_gateway.tws.contracts import forex, future, stock


def test_stock_default_smart_usd() -> None:
    contract = stock("AAPL")
    assert contract.symbol == "AAPL"
    assert contract.exchange == "SMART"
    assert contract.currency == "USD"
    assert contract.secType == "STK"


def test_stock_with_custom_exchange_and_currency() -> None:
    contract = stock("SAP", exchange="IBIS", currency="EUR")
    assert contract.exchange == "IBIS"
    assert contract.currency == "EUR"


def test_stock_with_primary_exchange() -> None:
    contract = stock("AAPL", primary_exchange="NASDAQ")
    assert contract.primaryExchange == "NASDAQ"


def test_stock_without_primary_exchange_leaves_field_default() -> None:
    contract = stock("AAPL")
    assert contract.primaryExchange == ""


def test_forex_default_idealpro() -> None:
    contract = forex("EURUSD")
    assert contract.secType == "CASH"
    assert contract.exchange == "IDEALPRO"


def test_forex_custom_exchange() -> None:
    contract = forex("EURUSD", exchange="FXSUBPIP")
    assert contract.exchange == "FXSUBPIP"


def test_future_with_contract_month() -> None:
    contract = future("ES", "CME", last_trade_date_or_contract_month="202612")
    assert contract.symbol == "ES"
    assert contract.exchange == "CME"
    assert contract.currency == "USD"
    assert contract.lastTradeDateOrContractMonth == "202612"
    assert contract.secType == "FUT"


def test_future_without_contract_month_leaves_empty() -> None:
    contract = future("ES", "CME")
    assert contract.lastTradeDateOrContractMonth == ""


def test_future_custom_currency() -> None:
    contract = future("FDAX", "EUREX", currency="EUR")
    assert contract.currency == "EUR"
