"""Currency-Normalisierung.

Single Source of Truth fuer Geldfeldern (Section 1.9 im API-Draft).
Geldwerte werden immer als Pydantic-`Money` mit String-`value` (gegen
Float-Rounding) und ISO-4217-`currency` repraesentiert. Andere Module
duerfen keine eigenen Money-Repraesentationen einfuehren - dieser
Wrapper ist Pflicht.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Money(BaseModel):
    value: str = Field(description="Decimal-String, kein Float (verhindert Rounding-Drift)")
    currency: str = Field(min_length=3, max_length=3, description="ISO-4217-Code (USD, EUR, ...)")

    @field_validator("currency")
    @classmethod
    def _currency_upper(cls, v: str) -> str:
        return v.upper()


def normalize_money(value: Any, currency: str | None) -> Money | None:
    """Konvertiert (value, currency) in ein `Money`-Objekt.

    Verhalten:
    - `value=None` oder `currency=None`/leer → `None`. Geldfelder sind
      optional; wenn der Caller (oder das CP-Gateway) keine Information
      hat, soll der Endpunkt das nicht erfinden.
    - Numerische Inputs (int/float/Decimal) werden ueber `Decimal` in
      String konvertiert; Strings werden uebernommen, sofern sie sich
      als Decimal parsen lassen (sonst ValueError).
    """
    if value is None or currency is None or not currency.strip():
        return None
    decimal_value = _to_decimal(value)
    return Money(value=str(decimal_value), currency=currency.strip().upper())


def normalize_summary_money(field: Any) -> Money | None:
    """Konvertiert ein IBKR-Summary-Feld in ein `Money`-Objekt.

    IBKR `/portfolio/{accountId}/summary` liefert pro Kennzahl ein Objekt
    der Form ``{"amount": <num>, "currency": <iso>, "value": <str>,
    "isNull": <bool>, "timestamp": <int>}``. Hier zaehlt nur amount und
    currency; value ist eine vorformatierte Stringdarstellung von amount
    und wird ignoriert. ``isNull=True`` oder fehlendes ``amount``/
    ``currency`` -> None.
    """
    if not isinstance(field, dict):
        return None
    if field.get("isNull") is True:
        return None
    amount = field.get("amount")
    currency = field.get("currency")
    if amount is None or currency is None:
        return None
    return normalize_money(amount, currency)


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise ValueError("bool ist kein Geldwert")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value.strip())
    raise ValueError(f"unsupported money value type: {type(value).__name__}")
