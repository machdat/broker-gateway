"""Sicherheits-Layer fuer Paper-Tests (AP-07 K3).

Garantiert, dass Paper-Tests nur gegen Paper-Konten und in einem
beschraenkten Wirkungsraum ausgefuehrt werden. Wird von actions.py vor
jeder Schreib-Operation angefragt.

Zwei Garantien

1. **Account-Whitelist.** Nur Konten mit ``DU``-Praefix werden
   akzeptiert. Live-Konto-IDs (``U25235077`` etc.) werden hart
   abgelehnt.
2. **Notional-Limits.** Pro Order ein konfigurierbares Maximum
   (Default 1 USD), pro Test-Lauf ein kumuliertes Maximum
   (Default 10 USD). Beide ueberschreitbar via ENV
   (``BG_PAPER_MAX_NOTIONAL_USD``, ``BG_PAPER_MAX_NOTIONAL_TOTAL_USD``)
   - aber nur in der Paper-Test-Umgebung, nie in CI-Default.
"""
from __future__ import annotations

import os
from decimal import Decimal


_DEFAULT_MAX_NOTIONAL_PER_ORDER = Decimal("1.00")
_DEFAULT_MAX_NOTIONAL_TOTAL = Decimal("10.00")


class PaperSafetyError(Exception):
    """Test-Operation widerspricht den Paper-Safety-Garantien."""


def assert_paper_account(account_id: str | None) -> str:
    """Prueft, dass ``account_id`` ein Paper-Konto ist (``DU``-Praefix).

    Liefert die normalisierte Account-ID zurueck. Wirft
    ``PaperSafetyError``, wenn der Wert fehlt oder kein ``DU``-Praefix
    hat.
    """
    if not account_id:
        raise PaperSafetyError(
            "BG_PAPER_ACCOUNT_ID nicht gesetzt - Paper-Tests benoetigen "
            "ein DU-Konto."
        )
    normalised = account_id.strip()
    if not normalised.upper().startswith("DU"):
        raise PaperSafetyError(
            f"Account-ID '{normalised}' hat keinen DU-Praefix - Paper-"
            "Tests duerfen nur gegen Paper-Konten laufen."
        )
    return normalised


def _decimal_from_env(name: str, default: Decimal) -> Decimal:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return Decimal(raw)
    except Exception:  # noqa: BLE001
        return default


def assert_within_paper_limits(
    *,
    notional_usd: Decimal,
    cumulative_notional_usd: Decimal,
) -> None:
    """Wirft ``PaperSafetyError`` bei Verletzung der Notional-Limits.

    ``notional_usd`` ist der Wert der gerade angefragten Order;
    ``cumulative_notional_usd`` ist die Summe aller bisherigen
    Order-Notionals dieses Test-Laufs (inkl. der gerade angefragten).
    """
    per_order_limit = _decimal_from_env(
        "BG_PAPER_MAX_NOTIONAL_USD", _DEFAULT_MAX_NOTIONAL_PER_ORDER
    )
    total_limit = _decimal_from_env(
        "BG_PAPER_MAX_NOTIONAL_TOTAL_USD", _DEFAULT_MAX_NOTIONAL_TOTAL
    )
    if notional_usd > per_order_limit:
        raise PaperSafetyError(
            f"Order-Notional ${notional_usd} > Per-Order-Limit "
            f"${per_order_limit} (BG_PAPER_MAX_NOTIONAL_USD)."
        )
    if cumulative_notional_usd > total_limit:
        raise PaperSafetyError(
            f"Kumulative Test-Notional ${cumulative_notional_usd} > "
            f"Test-Limit ${total_limit} "
            "(BG_PAPER_MAX_NOTIONAL_TOTAL_USD)."
        )


__all__ = [
    "PaperSafetyError",
    "assert_paper_account",
    "assert_within_paper_limits",
]
