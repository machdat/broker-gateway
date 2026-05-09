"""Helper-Funktionen fuer ``ib_async.Contract``-Instanzen.

Pendant zu :mod:`broker_gateway.cp.instruments`. Die Helper sind dunne
Wrapper um die ``ib_async``-Konstruktoren - sie existieren, damit der
Adapter-Code einen stabilen Eintrittspunkt hat (z.B. fuer Tests, die
``contracts.stock(...)`` mocken statt direkt ``ib_async.Stock(...)``)
und damit die Default-Werte ein gemeinsames Set bilden.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ib_async.contract import Contract


def stock(
    symbol: str,
    exchange: str = "SMART",
    currency: str = "USD",
    *,
    primary_exchange: str | None = None,
) -> Contract:
    """Erzeugt einen ``Stock``-Contract.

    ``primary_exchange`` ist fuer SMART-Routing bei Cross-Listed-
    Symbolen wichtig (z.B. ``AAPL`` als ``NASDAQ`` vs ``ARCA``).
    """
    from ib_async import Stock

    contract = Stock(symbol, exchange, currency)
    if primary_exchange:
        contract.primaryExchange = primary_exchange
    return contract


def forex(pair: str, *, exchange: str = "IDEALPRO") -> Contract:
    """Erzeugt einen ``Forex``-Contract (z.B. ``EURUSD``).

    ``exchange`` defaulted auf ``IDEALPRO`` (wie ``ib_async.Forex``).
    """
    from ib_async import Forex

    return Forex(pair, exchange=exchange)


def future(
    symbol: str,
    exchange: str,
    *,
    last_trade_date_or_contract_month: str | None = None,
    currency: str = "USD",
) -> Contract:
    """Erzeugt einen ``Future``-Contract.

    ``last_trade_date_or_contract_month`` im IBKR-Format: entweder
    ``YYYYMM`` (Contract-Monat, z.B. ``"202612"``) oder ``YYYYMMDD``
    (genauer Last-Trade-Tag). Wer den Contract erst qualifizieren
    will, kann auch ohne Datum starten und den Front-Month durch
    :meth:`broker_gateway.tws.client.TWSClient.qualify` aufloesen
    lassen.
    """
    from ib_async import Future

    return Future(
        symbol=symbol,
        lastTradeDateOrContractMonth=last_trade_date_or_contract_month or "",
        exchange=exchange,
        currency=currency,
    )
