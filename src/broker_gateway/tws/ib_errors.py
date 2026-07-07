"""Gemeinsame ib_async-Fehlermechanik fuer die TWS-Adapter (Karte dbc5edd7).

ib_async wirft mit ``RaiseRequestErrors=False`` (Projekt-Default, in ``src/``
nirgends ueberschrieben) bei IBKR-Request-Fehlern KEINE Exception. Der Wrapper
loest das Future stattdessen mit einem leeren Ergebnis auf und emittiert den
Fehler ausschliesslich ueber ``ib.errorEvent`` (``wrapper.error()`` ->
``self.ib.errorEvent.emit(reqId, errorCode, errorString, contract)``, immer die
letzte Zeile; der Nicht-Warning-Fall ruft ``_endReq(reqId)`` ohne Exception).

Damit sind ``try/except``-basierte Fehlerpfade fuer IBKR-Business-Rejections
(Pacing 162, 200 no security definition, 10162 market data not subscribed)
toter Code: der Endpunkt liefert still ``200`` mit leeren Records statt eines
Fehlerstatus (Silent-Failure). Dieses Modul kapselt den korrekten Pfad:

1. :func:`collect_request_errors` haengt einen Sammler an ``errorEvent`` und
   nimmt ihn nach dem Request wieder ab (Contextmanager, leak-frei).
2. :func:`first_fatal_ib_error` filtert die gesammelten Events nach der
   eigenen ``reqId`` (fremde Requests am geteilten IB-Handle werden ignoriert)
   und laesst benigne Codes durch (kein Fehler).
3. Das endpunktspezifische HTTP-Status-Mapping (Scanner vs. Historik) bleibt
   bewusst beim jeweiligen Adapter - die Codes unterscheiden sich.

Der Scanner (``tws/scanner.py``) und der Historik-Adapter (``tws/historical.py``)
teilen sich diesen Helper (DRY, ein einziger Fix-Ort).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any


# IBKR-Fehlercodes, die ib_async als Warnings behandelt (wrapper.error,
# warningCodes) und die fuer einen Request KEIN echter Fehler sind. 165 =
# "no longer matching results" = legitim leeres Ergebnis.
IB_REQUEST_WARNING_CODES: frozenset[int] = frozenset(
    {105, 110, 165, 321, 329, 399, 404, 434, 492, 10167}
)


def is_benign_ib_code(code: int) -> bool:
    """True fuer IBKR-Codes, die fuer einen Request kein Fehler sind.

    Das sind die ib_async-Warning-Codes plus der 2100-2199-Block
    (System-/Connectivity-Meldungen wie "market data farm connection is OK").
    """
    return code in IB_REQUEST_WARNING_CODES or 2100 <= code < 2200


def first_fatal_ib_error(
    errors: list[tuple[int, int, str]], req_id: int | None
) -> tuple[int, str] | None:
    """Erster fataler ``(code, message)`` fuer die eigene ``req_id``, sonst None.

    Fehler-Events fremder ``reqId`` (parallele Requests am geteilten IB-Handle)
    werden uebersprungen, wenn ``req_id`` bekannt ist. ``req_id is None``
    betrachtet alle Events (fuer Requests ohne aus dem Ergebnis ableitbare
    reqId). Benigne Codes (:func:`is_benign_ib_code`) sind kein Fehler.
    """
    for event_req_id, code, message in errors:
        if req_id is not None and event_req_id != req_id:
            continue
        if is_benign_ib_code(code):
            continue
        return code, message
    return None


@contextmanager
def collect_request_errors(ib: Any) -> Iterator[list[tuple[int, int, str]]]:
    """Sammelt ``errorEvent``-Emissionen von ``ib`` fuer die Dauer des Blocks.

    Yieldet die Sammel-Liste ``[(reqId, errorCode, errorString), ...]``. Der
    Handler wird am ``errorEvent`` angemeldet und im ``finally`` wieder
    abgemeldet - auch bei einer Exception im Block -, damit kein Handler am
    langlebigen IB-Handle haengen bleibt (Leak + Cross-Talk zwischen Requests).
    """
    collected: list[tuple[int, int, str]] = []

    def _collect(
        req_id: int, error_code: int, error_string: str, *_: object
    ) -> None:
        collected.append((req_id, error_code, error_string))

    ib.errorEvent += _collect
    try:
        yield collected
    finally:
        ib.errorEvent -= _collect
