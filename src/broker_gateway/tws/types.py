"""Pydantic-Modelle, die ``ib_async``-Objekte auf das interne
broker-gateway-Schema mappen.

Phase 1: Skelett-Modelle (nur Felder). Phase 2: Mapping-Funktionen
und Decimal/UTC/Currency-Disziplin.

Alle Modelle koexistieren mit den HTTP-Schemas in
:mod:`broker_gateway.cp.normalize` - sie sollen 1:1 dieselben v1-API-
Antworten produzieren, damit der Cutover (Karte 6) keine
Schema-Aenderung beim Consumer ausloest.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class AccountField(BaseModel):
    """Ein Feld der Account-Summary.

    ``ib_async.AccountValue``: ``account``, ``tag``, ``value``,
    ``currency``, ``modelCode``. Phase 2 mappt das auf dieses Modell
    und rundet Decimal-Felder.
    """

    model_config = ConfigDict(frozen=True)

    account: str
    tag: str
    value: str
    currency: str | None = None
    model_code: str | None = None


class Position(BaseModel):
    """Offene Position in einem Account.

    Quelle: ``ib_async.PortfolioItem``.
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


class Bar(BaseModel):
    """Historische Bar.

    Quelle: ``ib_async.BarData``. Zeitstempel als UTC-aware ``datetime``
    (Phase 2 normalisiert die ``ib_async``-Stringform).
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


class Snapshot(BaseModel):
    """Marketdata-Snapshot. Quelle: ``ib_async.Ticker``.

    Felder mappen auf den Endzustand nach ``snapshot=True``-Roundtrip.
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


class Tick(BaseModel):
    """Tick-Event aus ``ticker.updateEvent``.

    Phase 2 emittiert pro Update ein ``Tick``-Snapshot.
    """

    model_config = ConfigDict(frozen=True)

    con_id: int
    symbol: str
    field: str
    value: Decimal | None = None
    size: Decimal | None = None
    timestamp: datetime
