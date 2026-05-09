"""Helper-Funktionen fuer ``ib_async.Contract``-Instanzen.

Pendant zu :mod:`broker_gateway.cp.instruments`. Phase 1 Skelett -
Phase 2 fuellt die Bodies und ergaenzt Forex/Future-Varianten.
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

    Phase 2 ruft ``ib_async.Stock(symbol, exchange, currency)`` und
    setzt ``primaryExchange`` falls angegeben - das ist fuer SMART-
    Routing bei Cross-Listed-Symbolen (z.B. AAPL auf NASDAQ vs ARCA)
    nuetzlich.
    """
    raise NotImplementedError("Phase 2 - Bodies")


def forex(pair: str) -> Contract:
    """Erzeugt einen ``Forex``-Contract (z.B. ``EURUSD``).

    Phase 2 ruft ``ib_async.Forex(pair)``.
    """
    raise NotImplementedError("Phase 2 - Bodies")


def future(
    symbol: str,
    exchange: str,
    *,
    last_trade_date_or_contract_month: str | None = None,
    currency: str = "USD",
) -> Contract:
    """Erzeugt einen ``Future``-Contract.

    Phase 2 ruft ``ib_async.Future(symbol, lastTradeDateOrContractMonth,
    exchange)`` und ergaenzt ``currency``.
    """
    raise NotImplementedError("Phase 2 - Bodies")
