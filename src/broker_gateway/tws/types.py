"""Pydantic-Modelle, die ``ib_async``-Objekte auf das interne
broker-gateway-Schema mappen.

Disziplin:

- Decimal fuer alle Geldbetraege und Mengen, nie float (verlorene
  Praezision bei Marketdata).
- Zeitstempel als UTC-aware ``datetime``. ``ib_async`` liefert je nach
  Endpoint mal naive datetimes, mal ``date``, mal Unix-Timestamps.
- ``nan`` aus ``ib_async``-Tickers wird zu ``None`` (Pydantic kann
  ``Decimal('nan')`` nicht serialisieren und es ist semantisch sowieso
  "kein Wert").
"""
from __future__ import annotations

import math
from datetime import UTC, date as date_, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict


# --------------------------------------------------------------------------
# Helper
# --------------------------------------------------------------------------

def _decimal_or_none(value: Any) -> Decimal | None:
    """``None`` fuer ``None`` und ``nan``, sonst ``Decimal``.

    ``ib_async`` defaulted viele numerische Ticker-Felder auf ``nan``
    (z.B. ``ticker.bid`` wenn keine Subscription). ``Decimal('nan')``
    bricht Pydantic-Validation an mehreren Stellen, also normalisieren
    wir hier auf ``None``.
    """
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    return Decimal(str(value))


def _to_utc(ts: datetime | date_ | None) -> datetime | None:
    """Normalisiert auf UTC-aware ``datetime``.

    - ``None`` bleibt ``None``.
    - ``date`` wird zu Mitternacht-UTC.
    - naive ``datetime`` wird als UTC interpretiert (ib_async dokumentiert
      so fuer Bar-Endzeiten, wenn keine TZ konfiguriert ist).
    - aware ``datetime`` wird in UTC konvertiert.
    """
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)
    return datetime(ts.year, ts.month, ts.day, tzinfo=UTC)


# --------------------------------------------------------------------------
# Modelle
# --------------------------------------------------------------------------

class AccountField(BaseModel):
    """Ein Feld der Account-Summary.

    Quelle: :class:`ib_async.AccountValue`.
    """

    model_config = ConfigDict(frozen=True)

    account: str
    tag: str
    value: str
    currency: str | None = None
    model_code: str | None = None

    @classmethod
    def from_account_value(cls, av: Any) -> AccountField:
        return cls(
            account=av.account,
            tag=av.tag,
            value=av.value,
            currency=av.currency or None,
            model_code=av.modelCode or None,
        )


class Position(BaseModel):
    """Offene Position in einem Account.

    Quelle: :class:`ib_async.PortfolioItem` (per ``IB.portfolio()`` nach
    ``reqAccountUpdatesAsync``). Liefert sowohl Lots+AvgCost als auch
    MarketPrice/MarketValue/PnL - daher PortfolioItem statt der
    schlankeren ``Position``-Klasse aus ``IB.reqPositionsAsync``.
    """

    model_config = ConfigDict(frozen=True)

    account: str
    con_id: int
    symbol: str
    sec_type: str
    exchange: str | None = None
    currency: str
    position: Decimal
    average_cost: Decimal
    market_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None

    @classmethod
    def from_portfolio_item(cls, pi: Any) -> Position:
        contract = pi.contract
        return cls(
            account=pi.account,
            con_id=int(contract.conId),
            symbol=contract.symbol,
            sec_type=contract.secType,
            exchange=contract.exchange or None,
            currency=contract.currency,
            position=Decimal(str(pi.position)),
            average_cost=Decimal(str(pi.averageCost)),
            market_price=_decimal_or_none(pi.marketPrice),
            market_value=_decimal_or_none(pi.marketValue),
            unrealized_pnl=_decimal_or_none(pi.unrealizedPNL),
            realized_pnl=_decimal_or_none(pi.realizedPNL),
        )


class Bar(BaseModel):
    """Historische Bar.

    Quelle: :class:`ib_async.BarData`. Zeitstempel UTC-normalisiert.
    """

    model_config = ConfigDict(frozen=True)

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    wap: Decimal | None = None
    bar_count: int | None = None

    @classmethod
    def from_bar_data(cls, bd: Any) -> Bar:
        ts = _to_utc(bd.date)
        if ts is None:
            raise ValueError("BarData.date darf nicht None sein")
        return cls(
            timestamp=ts,
            open=Decimal(str(bd.open)),
            high=Decimal(str(bd.high)),
            low=Decimal(str(bd.low)),
            close=Decimal(str(bd.close)),
            volume=Decimal(str(bd.volume)),
            wap=_decimal_or_none(bd.average) if bd.average else None,
            bar_count=int(bd.barCount) if bd.barCount > 0 else None,
        )


class Snapshot(BaseModel):
    """Marketdata-Snapshot.

    Quelle: :class:`ib_async.Ticker` nach ``reqMktData(snapshot=True)``-
    Roundtrip.
    """

    model_config = ConfigDict(frozen=True)

    con_id: int
    symbol: str
    last: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bid_size: Decimal | None = None
    ask_size: Decimal | None = None
    volume: Decimal | None = None
    market_data_type: int | None = None
    timestamp: datetime | None = None

    @classmethod
    def from_ticker(cls, ticker: Any) -> Snapshot:
        contract = ticker.contract
        return cls(
            con_id=int(contract.conId) if contract is not None else 0,
            symbol=contract.symbol if contract is not None else "",
            last=_decimal_or_none(ticker.last),
            bid=_decimal_or_none(ticker.bid),
            ask=_decimal_or_none(ticker.ask),
            bid_size=_decimal_or_none(ticker.bidSize),
            ask_size=_decimal_or_none(ticker.askSize),
            volume=_decimal_or_none(ticker.volume),
            market_data_type=int(ticker.marketDataType) if ticker.marketDataType else None,
            timestamp=_to_utc(ticker.time),
        )


class Tick(BaseModel):
    """Tick-Event aus ``ticker.updateEvent``.

    Vereinfacht: pro Update emittiert der Stream einen ``Tick`` mit dem
    aktuellen ``last``-Preis und Timestamp. Granulare Tick-Klassifikation
    (Last/Bid/Ask) kommt erst, wenn ``reqTickByTickData`` gefragt ist -
    fuer den jetzigen Read-Pfad reicht das Ticker-Snapshot-Pattern.
    """

    model_config = ConfigDict(frozen=True)

    con_id: int
    symbol: str
    field: str
    value: Decimal | None = None
    size: Decimal | None = None
    timestamp: datetime

    @classmethod
    def from_ticker(cls, ticker: Any, *, field: str = "last") -> Tick:
        contract = ticker.contract
        ts = _to_utc(ticker.time) or datetime.now(UTC)
        if field == "bid":
            value = _decimal_or_none(ticker.bid)
            size = _decimal_or_none(ticker.bidSize)
        elif field == "ask":
            value = _decimal_or_none(ticker.ask)
            size = _decimal_or_none(ticker.askSize)
        else:
            value = _decimal_or_none(ticker.last)
            size = _decimal_or_none(ticker.lastSize)
        return cls(
            con_id=int(contract.conId) if contract is not None else 0,
            symbol=contract.symbol if contract is not None else "",
            field=field,
            value=value,
            size=size,
            timestamp=ts,
        )
