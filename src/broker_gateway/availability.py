"""Mapping IBKR-Availability-Codes -> menschenlesbare Kategorien.

Single Source of Truth für die 6509-Code-Normalisierung. Endpunkte und
Adapter dürfen kein eigenes Inline-Mapping schreiben - immer
`map_availability()` aufrufen.

Kontext: IBKR liefert im Feld 6509 dreistellige Codes wie `RPB`
(Real-time Paid Bidask), `DPB` (Delayed Paid Bidask), `ZB` / `ZF`
(Frozen). Laut docs/research/ibkr-cpapi-doc.json kennt das Feld die
Prefix-Buchstaben:

    R = RealTime, D = Delayed, Z = Frozen, Y = Frozen Delayed,
    N = Not Subscribed.

Der erste Buchstabe trägt die für Consumer relevante Information; die
letzten zwei beschreiben Subscription- und Bidask-Typ und sind aktuell
nicht relevant. `F` ist im offiziellen 6509-Schema nicht vorgesehen,
wird aber pragmatisch ebenfalls auf `frozen` gemappt - manche
hartcodierten Mock-Antworten verwenden `FPB` historisch.
"""
from __future__ import annotations

from typing import Literal


Availability = Literal["realtime", "delayed", "frozen"]


_PREFIX_MAP: dict[str, Availability] = {
    "R": "realtime",
    "D": "delayed",
    # Z = Frozen laut docs/research/ibkr-cpapi-doc.json (Feld 6509).
    # Y = Frozen Delayed wird auf `frozen` reduziert, bis Konsumenten
    # eine eigene Kategorie brauchen.
    "Z": "frozen",
    "Y": "frozen",
    # Legacy / Mock-Pragmatismus: F war in fruehen Snapshots gesetzt.
    "F": "frozen",
}


def map_availability(code: str | None) -> Availability | None:
    """Normalisiert einen IBKR 6509-Code auf realtime/delayed/frozen.

    Liefert `None` für unbekannte Codes (leerer String, fehlender Prefix-
    Buchstabe). `availability_raw` bleibt in Endpunkt-Antworten erhalten,
    damit Consumer im Sonderfall weiterhin den Originalcode sehen.
    """
    if not code:
        return None
    return _PREFIX_MAP.get(code[0].upper())
