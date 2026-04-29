"""Single Source of Truth fuer Header-Redaktion im IBKR-Verkehr.

Diese Liste der zu schwaerzenden Header wird vom CPRecorder, vom kommenden
CP-Wire-Logger und von der Inbound-Body-Middleware geteilt. Wer einen
neuen Sink fuer Verkehrsdaten baut, MUSS hier importieren - keine
lokalen Kopien.
"""
from __future__ import annotations

from typing import Iterable, Mapping


REDACTED_HEADERS: frozenset[str] = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
    "proxy-authorization",
})


def filter_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> dict[str, str]:
    """Liefert die Header ohne die in :data:`REDACTED_HEADERS` gefuehrten Felder.

    Akzeptiert ``httpx.Headers``, ``dict[str, str]`` oder eine beliebige
    Mapping-aehnliche Sequenz von ``(name, value)``-Paaren. Vergleich ist
    case-insensitive (REDACTED_HEADERS ist lower-case).
    """
    items = headers.items() if hasattr(headers, "items") else list(headers)
    return {
        name: value
        for name, value in items
        if name.lower() not in REDACTED_HEADERS
    }


__all__ = ["REDACTED_HEADERS", "filter_headers"]
