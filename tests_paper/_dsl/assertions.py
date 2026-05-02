"""Domain-Assertions fuer Paper-Tests (AP-07 K4).

Schmale Helfer fuer wiederkehrende Pruefungen rund um Money, Order-
Status und Side. Bewusst minimal - jeder Helfer hat einen klaren
Single-Reason-to-Fail.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Iterable


_VALID_SIDES = {"BUY", "SELL"}


def assert_money_close(
    actual: Decimal | float | int | str | None,
    expected: Decimal | float | int | str,
    *,
    tolerance: Decimal | float = Decimal("0.01"),
    label: str = "money",
) -> None:
    """Vergleicht zwei Money-Werte mit numerischer Toleranz.

    Beide Werte werden in Decimal konvertiert; wenn ``actual`` None
    ist, wird mit AssertionError abgelehnt. ``tolerance`` darf Decimal
    oder float sein.
    """
    if actual is None:
        raise AssertionError(f"{label}: erwartet ~{expected}, bekam None")
    actual_d = Decimal(str(actual))
    expected_d = Decimal(str(expected))
    tol_d = Decimal(str(tolerance))
    delta = abs(actual_d - expected_d)
    if delta > tol_d:
        raise AssertionError(
            f"{label}: erwartet ~{expected_d} (+/- {tol_d}), bekam "
            f"{actual_d} (Delta {delta})"
        )


def assert_order_status_in(
    status: str | None,
    expected: Iterable[str],
    *,
    label: str = "status",
) -> None:
    """Stellt sicher, dass ``status`` einer der erwarteten Werte ist.

    ``status`` wird auf lowercase normalisiert; ``expected`` darf
    Mix-Case sein. None oder Leer-String werden abgelehnt.
    """
    if not status:
        raise AssertionError(f"{label}: erwartet {sorted(expected)}, bekam None")
    expected_norm = {s.lower() for s in expected}
    actual_norm = status.strip().lower()
    if actual_norm not in expected_norm:
        raise AssertionError(
            f"{label}: erwartet {sorted(expected_norm)}, bekam "
            f"{actual_norm!r}"
        )


def assert_side_valid(side: str | None) -> None:
    """Akzeptiert nur ``BUY`` / ``SELL`` (case-insensitive)."""
    if not side:
        raise AssertionError("side: leer / None")
    if side.strip().upper() not in _VALID_SIDES:
        raise AssertionError(
            f"side: erwartet {sorted(_VALID_SIDES)}, bekam {side!r}"
        )


__all__ = [
    "assert_money_close",
    "assert_order_status_in",
    "assert_side_valid",
]
