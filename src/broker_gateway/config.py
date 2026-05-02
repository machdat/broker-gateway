"""Schmale ENV-Konfigurationsschicht.

Single Source of Truth fuer ENV-Variablen, die quer durch den Service
gelesen werden. Alle ``BG_``-praefixierten Werte mit Default-Fallback.

Bewusst nicht: pydantic-Settings, Plugin-Discovery, hierarchische
Config-Files. Der Service ist klein genug, dass ``os.environ.get`` mit
Wrapper-Funktion reicht.
"""
from __future__ import annotations

import logging
import os
from typing import Final, Literal


logger = logging.getLogger(__name__)


# Wert von ``BG_QUOTES_SOURCE``: ``ws`` aktiviert den WSPushSource-Pfad,
# ``polling`` belaesst es bei dem klassischen REST-Polling.
QuotesSource = Literal["ws", "polling"]


_QUOTES_SOURCE_ENV: Final[str] = "BG_QUOTES_SOURCE"
_QUOTES_SOURCE_DEFAULT: Final[QuotesSource] = "polling"
_QUOTES_SOURCE_VALID: Final[frozenset[str]] = frozenset({"ws", "polling"})


def quotes_source() -> QuotesSource:
    """Liefert die Quelle fuer den ``/v1/quotes/stream``-SSE-Pfad.

    Default ist ``polling`` (Bestand), nicht ``ws`` wie das K6-Design
    vorsieht. Begruendung: der WS-Pfad braucht eine aktive
    ``CPWebSocketClient``-Connection und einen geloggten Account; vor
    dem 2FA-Login (Cold-Start) ist das nicht verfuegbar. Mit Default
    ``polling`` laeuft der Service ohne ENV-Pflege wie heute, ``ws``
    ist Opt-in. Sobald der WS-Pfad einen Trading-Tag stabil laeuft
    (Phase-A-Deploy-Bedingung gemaess Sektion 6.2 des K6-Designs),
    kann der Default in einer Folge-Karte auf ``ws`` springen.
    """
    raw = os.environ.get(_QUOTES_SOURCE_ENV, _QUOTES_SOURCE_DEFAULT).strip().lower()
    if raw not in _QUOTES_SOURCE_VALID:
        logger.warning(
            "ENV %s=%r ist keiner von %s, fallback auf %s",
            _QUOTES_SOURCE_ENV,
            raw,
            sorted(_QUOTES_SOURCE_VALID),
            _QUOTES_SOURCE_DEFAULT,
        )
        return _QUOTES_SOURCE_DEFAULT
    return raw  # type: ignore[return-value]


__all__ = ["QuotesSource", "quotes_source"]
