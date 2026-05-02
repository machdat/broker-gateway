"""Sicherheits-Layer fuer Paper-Tests (AP-07 K3).

Garantiert, dass Paper-Tests nur gegen Paper-Konten und in einem
beschraenkten Wirkungsraum ausgefuehrt werden. Wird von actions.py vor
jeder Schreib-Operation angefragt.

Vier Garantien

1. **Account-Whitelist.** Nur Konten mit ``DU``-Praefix werden
   akzeptiert; zusaetzlich darf eine komma-separierte Liste in
   ``BG_PAPER_ACCOUNT_WHITELIST`` weitere Test-Konten freischalten.
   Live-Konto-IDs (``U25235077`` etc.) werden hart abgelehnt.
2. **Per-Order-Notional.** ``max_notional_per_order`` blockt grosse
   Einzel-Orders (Default 500 USD, ENV
   ``BG_PAPER_MAX_NOTIONAL_PER_ORDER``).
3. **Kumulatives Test-Notional.** ``assert_within_paper_limits``
   blockt Test-Laeufe, in denen die Summe aller Order-Notionals einen
   Schwellenwert ueberschreitet (Default 10 USD, ENV
   ``BG_PAPER_MAX_NOTIONAL_TOTAL_USD``).
4. **Open-Orders-Cap.** ``max_open_orders`` checkt die Anzahl
   aktuell offener Orders gegen ``BG_PAPER_MAX_OPEN_ORDERS`` (Default 5).
   Schutz gegen kaskadierende place-Tests, die in einer Schleife
   haengen bleiben.

Plus ``kill_switch_active`` als Modul-Helper fuer den
``BG_PAPER_TESTS_DISABLED``-ENV (case-insensitive).
"""
from __future__ import annotations

import os
from decimal import Decimal
from typing import Any, Protocol


_DEFAULT_MAX_NOTIONAL_PER_ORDER = Decimal("500.00")
_DEFAULT_MAX_NOTIONAL_TOTAL = Decimal("10.00")
_DEFAULT_MAX_OPEN_ORDERS = 5
_KILL_SWITCH_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


class _OpenOrdersClient(Protocol):
    async def get(self, path: str, /, **kwargs: Any) -> Any:  # pragma: no cover
        ...


class PaperSafetyError(Exception):
    """Test-Operation widerspricht den Paper-Safety-Garantien."""


def assert_paper_account(account_id: str | None) -> str:
    """Prueft, dass ``account_id`` ein Paper-Konto ist (``DU``-Praefix).

    Akzeptiert zusaetzlich Konten, die in
    ``BG_PAPER_ACCOUNT_WHITELIST`` (komma-separiert) eingetragen sind -
    fuer den Fall, dass IBKR ausnahmsweise ein Test-Konto ohne
    DU-Praefix vergibt.

    Liefert die normalisierte Account-ID zurueck. Wirft
    ``PaperSafetyError``, wenn der Wert fehlt oder weder DU-Praefix
    noch Whitelist-Treffer.
    """
    if not account_id:
        raise PaperSafetyError(
            "BG_PAPER_ACCOUNT_ID nicht gesetzt - Paper-Tests benoetigen "
            "ein DU-Konto."
        )
    normalised = account_id.strip()
    if normalised.upper().startswith("DU"):
        return normalised
    whitelist_raw = os.environ.get("BG_PAPER_ACCOUNT_WHITELIST", "")
    whitelist = {entry.strip() for entry in whitelist_raw.split(",") if entry.strip()}
    if normalised in whitelist:
        return normalised
    raise PaperSafetyError(
        f"Account-ID '{normalised}' hat keinen DU-Praefix und steht "
        "nicht in BG_PAPER_ACCOUNT_WHITELIST - Paper-Tests duerfen nur "
        "gegen Paper-Konten laufen."
    )


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


def max_notional_per_order(
    price: Decimal | float | int | str,
    quantity: Decimal | float | int | str,
    *,
    env: str = "BG_PAPER_MAX_NOTIONAL_PER_ORDER",
    default: Decimal | float = _DEFAULT_MAX_NOTIONAL_PER_ORDER,
) -> None:
    """Wirft ``PaperSafetyError``, wenn ``price * quantity`` den
    Schwellenwert ueberschreitet.

    Default 500 USD. Override via ENV ``BG_PAPER_MAX_NOTIONAL_PER_ORDER``.
    """
    notional = Decimal(str(price)) * Decimal(str(quantity))
    threshold = _decimal_from_env(env, Decimal(str(default)))
    if notional > threshold:
        raise PaperSafetyError(
            f"Per-Order-Notional ${notional} > Schwelle ${threshold} "
            f"({env})."
        )


async def max_open_orders(
    http_client: _OpenOrdersClient,
    account_id: str,
    *,
    env: str = "BG_PAPER_MAX_OPEN_ORDERS",
    default: int = _DEFAULT_MAX_OPEN_ORDERS,
) -> None:
    """Holt offene Orders via ``GET /v1/orders?account=...`` und wirft
    ``PaperSafetyError``, wenn deren Anzahl den Schwellenwert
    ueberschreitet.

    Schutz gegen kaskadierende Test-Loops, die in einer Endlos-place-
    Schleife haengen bleiben. Liefert ohne weiteres Verhalten zurueck,
    wenn der Endpoint nicht 200 antwortet (defensiv: kein Test-Crash
    nur weil die Diagnose nicht ging).
    """
    threshold_raw = os.environ.get(env, str(default))
    try:
        threshold = int(threshold_raw)
    except ValueError:
        threshold = default
    response = await http_client.get(
        "/v1/orders", params={"account": account_id}
    )
    status_code = getattr(response, "status_code", 0)
    if status_code != 200:
        return
    body = response.json()
    if isinstance(body, dict):
        items = body.get("orders") or []
    elif isinstance(body, list):
        items = body
    else:
        items = []
    if len(items) > threshold:
        raise PaperSafetyError(
            f"{len(items)} offene Orders > Schwelle {threshold} "
            f"({env}) - Test-Loop verdaechtig."
        )


def kill_switch_active() -> bool:
    """Liefert True, wenn ``BG_PAPER_TESTS_DISABLED`` aktiv ist
    (case-insensitiv: 1/true/yes/on)."""
    raw = os.environ.get("BG_PAPER_TESTS_DISABLED", "").strip().lower()
    return raw in _KILL_SWITCH_TRUE_VALUES


__all__ = [
    "PaperSafetyError",
    "assert_paper_account",
    "assert_within_paper_limits",
    "kill_switch_active",
    "max_notional_per_order",
    "max_open_orders",
]
