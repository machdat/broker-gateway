"""Order-Pydantic-Models.

Single Source of Truth fuer Order-Validierung. Andere Module
(api/v1/orders.py, cp/orders.py) duerfen kein eigenes Schema
definieren - sie nutzen ausschliesslich `OrderRequest` (eingehend) und
`Order` (ausgehend).

Out-of-Scope fuer v1: OCA/OCO-Bracket-Orders, Trailing-Stops,
Smart-Routing-Hints. Diese Felder werden vom Server aktiv abgelehnt
oder ignoriert, je nach Endpunkt-Doku.
"""
from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from broker_gateway.money import Money


class OrderType(str, enum.Enum):
    LMT = "LMT"
    MKT = "MKT"
    STP = "STP"
    STP_LMT = "STP-LMT"


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(str, enum.Enum):
    DAY = "DAY"
    GTC = "GTC"


class OrderStatus(str, enum.Enum):
    PENDING_SUBMIT = "PendingSubmit"
    SUBMITTED = "Submitted"
    FILLED = "Filled"
    CANCELLED = "Cancelled"
    REJECTED = "Rejected"
    INACTIVE = "Inactive"


class OrderRequest(BaseModel):
    """Eingehende Order. Validierung haengt vom OrderType ab.

    Pflichtfelder pro Typ:
    - `LMT`     : limit_price
    - `STP`     : stop_price
    - `STP-LMT` : limit_price + stop_price
    - `MKT`     : keiner

    Quantity ist Decimal-faehig (IBKR erlaubt fraktionale Stuecke fuer
    bestimmte Instrumente). Negative Mengen werden abgelehnt - die
    Richtung ergibt sich aus `side`.
    """

    account_id: str = Field(min_length=1)
    conid: int = Field(gt=0)
    side: OrderSide
    quantity: str = Field(description="Decimal-String, > 0")
    order_type: OrderType
    tif: TimeInForce = TimeInForce.DAY
    limit_price: str | None = Field(default=None, description="Decimal-String, falls LMT/STP-LMT")
    stop_price: str | None = Field(default=None, description="Decimal-String, falls STP/STP-LMT")

    @field_validator("quantity")
    @classmethod
    def _quantity_positive(cls, v: str) -> str:
        try:
            value = Decimal(v)
        except Exception as exc:
            raise ValueError(f"quantity ist kein Decimal: {v!r}") from exc
        if value <= 0:
            raise ValueError("quantity muss > 0 sein")
        return str(value)

    @field_validator("limit_price", "stop_price")
    @classmethod
    def _price_decimal_if_set(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            value = Decimal(v)
        except Exception as exc:
            raise ValueError(f"price ist kein Decimal: {v!r}") from exc
        if value <= 0:
            raise ValueError("price muss > 0 sein")
        return str(value)

    @model_validator(mode="after")
    def _validate_price_combination(self) -> "OrderRequest":
        if self.order_type is OrderType.LMT and self.limit_price is None:
            raise ValueError("limit_price ist Pflicht fuer order_type=LMT")
        if self.order_type is OrderType.STP and self.stop_price is None:
            raise ValueError("stop_price ist Pflicht fuer order_type=STP")
        if self.order_type is OrderType.STP_LMT:
            if self.limit_price is None or self.stop_price is None:
                raise ValueError("STP-LMT erfordert sowohl limit_price als auch stop_price")
        if self.order_type is OrderType.MKT and (
            self.limit_price is not None or self.stop_price is not None
        ):
            raise ValueError("MKT-Order darf weder limit_price noch stop_price tragen")
        return self


class Order(BaseModel):
    """Ausgehende Order-Repraesentation.

    `status` folgt dem CP-Gateway-Vokabular (PendingSubmit/Submitted/
    Filled/Cancelled/Rejected/Inactive). Das mag spaeter normalisiert
    werden - bis dahin durchreichen.
    """

    order_id: str
    account_id: str
    conid: int
    side: OrderSide
    quantity: str
    order_type: OrderType
    tif: TimeInForce
    status: OrderStatus
    limit_price: str | None = None
    stop_price: str | None = None
    avg_fill_price: Money | None = None
    commission: Money | None = None
    filled_quantity: str | None = None
    submitted_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)


class OrderCancellation(BaseModel):
    order_id: str
    status: OrderStatus = OrderStatus.CANCELLED
    cancelled_at: datetime | None = None


__all__ = [
    "OrderType",
    "OrderSide",
    "TimeInForce",
    "OrderStatus",
    "OrderRequest",
    "Order",
    "OrderCancellation",
]
