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
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from broker_gateway.money import Money


# client_order_id -> IBKR-orderRef (Karte 0cfea205). Beide Grenzen sind
# gegen IBKR-Paper (DUQ312230) gemessen, nicht aus der Doku übernommen
# - Messprotokoll am 2026-07-16, Details in docs/api/v1.md Section 7.1.
#
# Länge: IBKR hat KEINE Grenze, die wir gefunden hätten - 1, 40, 64, 100
# und 500 Zeichen kamen alle byte-identisch zurück (permId als Beleg,
# dass IBKR die Order angenommen hat). Die 64 hier ist deshalb bewusst
# UNSERE Entscheidung und keine Broker-Grenze: ein Korrelationsschlüssel
# ist ein Schlüssel und kein Datenfeld, und eine UUID (36) mit Präfix
# passt bequem. Wer mehr braucht, kann die Zahl anheben - bis mindestens
# 500 Zeichen ist gemessen, dass IBKR nicht kürzt oder ablehnt.
_CLIENT_ORDER_ID_MAX_LEN = 64

# Zeichensatz: hier ist die Grenze NICHT verhandelbar. Ein orderRef mit
# Non-ASCII zerlegt den TWS-Wire-Stream - IBKR antwortet mit
# "Error 320: Attempted read beyond end of socket stream" und die Order
# wird NICHT platziert (permId bleibt 0). Isoliert nachgemessen: ASCII
# davor angenommen, Umlaut abgelehnt, ASCII danach wieder angenommen.
# Ohne diesen Guard verliert ein Aufrufer mit einem Umlaut im Schlüssel
# seine Order still - genau der stille Verlust, den die Karte ausschließt.
# Das TWS-Protokoll ist NUL-separiert; Multi-Byte-UTF-8 verschiebt die
# Feldgrenzen. Erlaubt ist druckbares ASCII (Space bis Tilde), was
# Control-Characters einschließlich NUL mit abdeckt.
#
# Anker \Z, NICHT $: Pythons $ matcht ohne re.MULTILINE auch unmittelbar
# VOR einem abschließenden \n. Mit $ rutschte ein "bot-task\n" durch den
# Guard und landete als orderRef mit Control-Character auf der Wire -
# genau die Zeichenklasse, die der Guard fernhalten soll. \Z ankert hart
# am String-Ende. (Karte 0cfea205, im Review gefunden.)
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^[\x20-\x7e]+\Z")


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
    client_order_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=_CLIENT_ORDER_ID_MAX_LEN,
        description=(
            "Aufrufer-eigener Korrelationsschlüssel, wird als IBKR-orderRef "
            "an den Broker durchgereicht und kommt auf Order und Stream-Frame "
            "zurück. Anders als order_id wird er beim Modify nicht neu "
            "vergeben. Druckbares ASCII, max "
            f"{_CLIENT_ORDER_ID_MAX_LEN} Zeichen. KEIN Idempotency-Key: IBKR "
            "erzwingt auf orderRef keine Eindeutigkeit (gemessen) - dafür ist "
            "der Idempotency-Key-Header da."
        ),
    )

    @field_validator("client_order_id")
    @classmethod
    def _client_order_id_printable_ascii(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not _CLIENT_ORDER_ID_PATTERN.match(v):
            raise ValueError(
                "client_order_id erlaubt nur druckbares ASCII (0x20-0x7e). "
                "Non-ASCII zerlegt den TWS-Wire-Stream: IBKR lehnt die Order "
                "dann mit 'Error 320: Attempted read beyond end of socket "
                "stream' ab, ohne sie zu platzieren."
            )
        return v

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
    def _positive_decimal_if_set(
        cls, v: str | None, info: ValidationInfo
    ) -> str | None:
        if v is None:
            return None
        field = info.field_name
        try:
            value = Decimal(v)
        except Exception as exc:
            raise ValueError(f"{field} ist kein Decimal: {v!r}") from exc
        if value <= 0:
            raise ValueError(f"{field} muss > 0 sein")
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
    client_order_id: str | None = Field(
        default=None,
        description=(
            "Der beim Platzieren mitgegebene Korrelationsschlüssel, wie ihn "
            "der Broker zurückmeldet (IBKR-orderRef). None, wenn keiner "
            "gesetzt wurde. Bewusst aus der Broker-Antwort gelesen und nicht "
            "aus dem Request gespiegelt: sonst würde das Feld einen Schlüssel "
            "bestätigen, der beim Broker nie ankam. Auf dem "
            "cp-legacy-Roll-Back-Profil immer None - dort wird der Schlüssel "
            "nicht durchgereicht."
        ),
    )
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
