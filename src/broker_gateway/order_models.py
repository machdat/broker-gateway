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


class OrderModifyRequest(BaseModel):
    """Eingehende Order-Modifikation (`PATCH /v1/orders/{order_id}`).

    Nur die mitgegebenen Preis-/Mengen-Felder werden auf die bestehende
    Order angewandt; unveraenderliche Eigenschaften (conid, side,
    order_type) bleiben. IBKR setzt die Aenderung als cancel/replace um
    (gleiche orderId). `account_id` ist Pflicht, weil die TWS-Session
    mehrere Konten halten kann. Mindestens eines der optionalen Felder
    muss gesetzt sein - ein leerer Modify ist sinnlos und wird abgelehnt.
    """

    account_id: str = Field(min_length=1)
    stop_price: str | None = Field(
        default=None, description="Neuer Stop-Trigger (auxPrice), Decimal-String > 0"
    )
    limit_price: str | None = Field(
        default=None, description="Neuer Limit-Preis (lmtPrice), Decimal-String > 0"
    )
    quantity: str | None = Field(
        default=None, description="Neue Menge, Decimal-String > 0"
    )

    @field_validator("stop_price", "limit_price", "quantity")
    @classmethod
    def _positive_decimal_if_set(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            value = Decimal(v)
        except Exception as exc:
            raise ValueError(f"Wert ist kein Decimal: {v!r}") from exc
        if value <= 0:
            raise ValueError("Wert muss > 0 sein")
        return str(value)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "OrderModifyRequest":
        if self.stop_price is None and self.limit_price is None and self.quantity is None:
            raise ValueError(
                "mindestens eines von stop_price/limit_price/quantity muss gesetzt sein"
            )
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
    stop_price: str | None = Field(
        default=None,
        description="Stop-Level (auxPrice) bei STP/STP-LMT — fuer den Konsumenten der Stop-Trigger.",
    )
    oca_group: str | None = Field(
        default=None,
        description=(
            "One-Cancels-All-Gruppe (IBKR ocaGroup), falls die Order Teil "
            "einer OCA-Gruppe ist (z.B. Bracket-/Stop-Take-Profit). None, "
            "wenn die Order keiner OCA-Gruppe angehoert."
        ),
    )
    avg_fill_price: Money | None = None
    commission: Money | None = None
    filled_quantity: str | None = None
    submitted_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)


class OrderCancellation(BaseModel):
    order_id: str
    status: OrderStatus = OrderStatus.CANCELLED
    cancelled_at: datetime | None = None


class WhatIfWarning(BaseModel):
    """Eine Pre-Trade-Warnung aus der What-If-Vorschau.

    `raw_id` ist der IBKR-Warning-Code (z.B. 21), sofern das Backend ihn
    liefert. Der ib_async/TWS-Pfad gibt nur Freitext (`warningText`) -
    dort bleibt `raw_id` `None` und `code` faellt auf `whatif_warning`
    zurueck, wenn kein bekannter Code abgeleitet werden kann.
    """

    code: str
    raw_id: int | None = None
    message: str


class MarginImpact(BaseModel):
    """Margin-/Funds-Auswirkung der vorgeschlagenen Order.

    Alle Felder sind optional - das Backend liefert sie nur, wenn der
    Account subscribed ist und die Base-Currency bekannt ist. Quelle im
    TWS-Pfad: `ib_async.OrderState` (equityWithLoan/initMargin/
    maintMargin before/after).
    """

    current_funds: Money | None = None
    after_funds: Money | None = None
    init_margin_after: Money | None = None
    maint_margin_after: Money | None = None


class WhatIfPreview(BaseModel):
    """Ausgehende What-If-/Margin-Vorschau (`POST /v1/orders/whatif`).

    Reine Vorschau - es wird keine Order platziert. Geldfelder sind
    optional, weil das Backend je nach Order-Typ und Marktdaten-Lage
    nicht alle Werte liefert (z.B. kein `estimated_amount` bei MKT ohne
    Limit-Preis).
    """

    account_id: str
    conid: int
    estimated_amount: Money | None = None
    estimated_commission: Money | None = None
    estimated_total: Money | None = None
    margin_impact: MarginImpact = Field(default_factory=MarginImpact)
    warnings: list[WhatIfWarning] = Field(default_factory=list)


__all__ = [
    "OrderType",
    "OrderSide",
    "TimeInForce",
    "OrderStatus",
    "OrderRequest",
    "OrderModifyRequest",
    "Order",
    "OrderCancellation",
    "WhatIfWarning",
    "MarginImpact",
    "WhatIfPreview",
]
