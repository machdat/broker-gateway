"""Domain-Assertions fuer Paper-Tests (AP-07 K4).

Schmale Helfer fuer wiederkehrende Pruefungen gegen den
``broker-gateway``-API-Vertrag (siehe ``docs/api/v1-draft.md``,
insb. Section 1.6 Error-Envelope und Section 9 Events). Jeder Helfer
hat einen klaren Single-Reason-to-Fail.

Funktionen
----------

- ``assert_money_close``: numerischer Vergleich Decimal mit Toleranz.
- ``assert_money_normalized``: Vertrag des Money-Wrappers
  ``{value, currency}``.
- ``assert_order_status_in`` / ``assert_side_valid``: einfache
  Wertebereichs-Checks.
- ``assert_event_envelope_v1``: SSE-Event-Body gemaess Section 9
  (data + type + id, optional retry).
- ``assert_idempotency_replay_returns_original``: schaerfster
  Idempotency-Lackmus-Test - Body-Identitaet bei gleichem Key.
- ``assert_error_envelope_v1``: Section-1.6-Schema (`error.code`,
  `error.message`, `error.request_id`).
- ``assert_pacing_headers_present``: Retry-After bei 429,
  X-RateLimit-* bei 200 (sofern vorhanden).
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Iterable


_VALID_SIDES = {"BUY", "SELL"}
_ALLOWED_EVENT_TYPES = frozenset({
    "execution",
    "order_status",
    "position",
})
_CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


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


def assert_money_normalized(
    field: Any, *, label: str = "money"
) -> None:
    """Prueft ``{value: number, currency: ISO-3-letter}``-Form.

    Akzeptiert ``int``/``float``/``Decimal`` als ``value`` und
    Strings/Decimals; ``currency`` muss exakt drei Grossbuchstaben
    haben (ISO 4217). Zusaetzliche Felder im Dict werden abgelehnt -
    der Vertrag ist strikt.
    """
    if not isinstance(field, dict):
        raise AssertionError(f"{label}: erwartet dict, bekam {type(field).__name__}")
    extra = set(field.keys()) - {"value", "currency"}
    if extra:
        raise AssertionError(
            f"{label}: unerwartete Felder {sorted(extra)} (erlaubt: value, currency)"
        )
    value = field.get("value")
    if value is None:
        raise AssertionError(f"{label}: 'value' fehlt")
    if not isinstance(value, (int, float, Decimal)) and not (
        isinstance(value, str)
        and _looks_like_decimal(value)
    ):
        raise AssertionError(
            f"{label}: 'value' muss Zahl oder numerischer String sein, "
            f"bekam {type(value).__name__} ({value!r})"
        )
    currency = field.get("currency")
    if not isinstance(currency, str) or not _CURRENCY_PATTERN.match(currency):
        raise AssertionError(
            f"{label}: 'currency' muss 3-Buchstaben-ISO-Code sein, "
            f"bekam {currency!r}"
        )


def _looks_like_decimal(value: str) -> bool:
    try:
        Decimal(value)
        return True
    except Exception:  # noqa: BLE001
        return False


def assert_event_envelope_v1(event: Any, *, label: str = "event") -> None:
    """SSE-Event-Body gemaess docs/api/v1-draft.md Section 9.

    Pflichtfelder ``data`` (object), ``type`` (string aus Whitelist),
    ``id`` (str|int). ``retry`` ist optional (int).
    """
    if not isinstance(event, dict):
        raise AssertionError(f"{label}: erwartet dict, bekam {type(event).__name__}")
    for required in ("data", "type", "id"):
        if required not in event:
            raise AssertionError(f"{label}: Pflichtfeld {required!r} fehlt")
    event_type = event["type"]
    if event_type not in _ALLOWED_EVENT_TYPES:
        raise AssertionError(
            f"{label}: type {event_type!r} nicht in Whitelist "
            f"{sorted(_ALLOWED_EVENT_TYPES)}"
        )
    if not isinstance(event["id"], (str, int)):
        raise AssertionError(
            f"{label}: id muss str oder int sein, bekam "
            f"{type(event['id']).__name__}"
        )
    if "retry" in event and not isinstance(event["retry"], int):
        raise AssertionError(
            f"{label}: retry muss int sein, bekam {type(event['retry']).__name__}"
        )


def assert_idempotency_replay_returns_original(
    original: Any,
    replay: Any,
    *,
    must_match_keys: Iterable[str] = ("order_id",),
) -> None:
    """Schaerfster Idempotency-Lackmus-Test: Body-Identitaet bei
    gleichem Idempotency-Key.

    Erwartung: ``replay`` liefert exakt denselben Body wie ``original``.
    Mindestens ``must_match_keys`` (Default ``order_id``) muessen
    bitidentisch sein - das beweist, dass der Idempotency-Cache
    tatsaechlich greift.
    """
    if isinstance(original, dict) and isinstance(replay, dict):
        for key in must_match_keys:
            if key not in original:
                raise AssertionError(
                    f"idempotency: Pflichtschluessel {key!r} fehlt im "
                    "Original-Body"
                )
            if original[key] != replay.get(key):
                raise AssertionError(
                    f"idempotency: {key!r} mismatch original={original[key]!r} "
                    f"replay={replay.get(key)!r}"
                )
        if original != replay:
            raise AssertionError(
                "idempotency: Body-Identitaet verletzt "
                f"(original keys {sorted(original)}, replay keys {sorted(replay)})"
            )
        return
    if original != replay:
        raise AssertionError(
            f"idempotency: original ({original!r}) != replay ({replay!r})"
        )


def assert_error_envelope_v1(
    response: Any,
    *,
    expected_code: str | None = None,
    label: str = "error",
) -> None:
    """Prueft das Section-1.6-Error-Schema und optional den Code.

    ``response`` darf ein dict (Body) oder ein httpx.Response-aehnliches
    Objekt mit ``.json()`` sein.
    """
    body = response.json() if hasattr(response, "json") and callable(response.json) else response
    if not isinstance(body, dict):
        raise AssertionError(f"{label}: erwartet dict, bekam {type(body).__name__}")
    error = body.get("error")
    if not isinstance(error, dict):
        raise AssertionError(
            f"{label}: 'error'-Objekt fehlt im Body (keys: {sorted(body)})"
        )
    for required in ("code", "message", "request_id"):
        if required not in error:
            raise AssertionError(
                f"{label}: 'error.{required}' fehlt"
            )
    if expected_code is not None and error["code"] != expected_code:
        raise AssertionError(
            f"{label}: erwartet code={expected_code!r}, bekam {error['code']!r}"
        )


def assert_pacing_headers_present(response: Any, *, label: str = "pacing") -> None:
    """Prueft Pacing-Header je nach Status-Code:

    - 429: ``Retry-After`` ist Pflicht.
    - 200: keine Pflicht, aber wenn vorhanden, soll ``X-RateLimit-*``
      konsistent sein. Hier nur 429-Pfad gepflegt; 200-Pfad ist
      Future-Karte.
    """
    status_code = getattr(response, "status_code", None)
    headers = {k.lower(): v for k, v in getattr(response, "headers", {}).items()}
    if status_code == 429:
        if "retry-after" not in headers:
            raise AssertionError(
                f"{label}: 429-Response ohne Retry-After-Header"
            )


__all__ = [
    "assert_error_envelope_v1",
    "assert_event_envelope_v1",
    "assert_idempotency_replay_returns_original",
    "assert_money_close",
    "assert_money_normalized",
    "assert_order_status_in",
    "assert_pacing_headers_present",
    "assert_side_valid",
]
