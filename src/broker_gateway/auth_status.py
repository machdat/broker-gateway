"""Zentrales AuthStatus-Enum fuer beide Backends (CP + TWS).

Vor diesem Modul lebte ``AuthStatus`` ausschliesslich in
``broker_gateway.cp.lifecycle``. Mit Karte 33cb35b1 (TWS-Lifecycle) wird
das Enum um zwei TWS-spezifische Werte erweitert (``TWS_DOWN``,
``SESSION_LOST``) und in dieses Modul gehoben.

Backward-Compat: ``broker_gateway.cp.lifecycle.AuthStatus`` ist ein
Re-Export von hier. Bestehender Code, der ``from broker_gateway.cp.lifecycle
import AuthStatus`` macht, funktioniert unveraendert.

Werte-Inventar:

- ``OK`` - Session steht, Adapter ist verbindbar (beide Backends).
- ``REAUTH_PENDING`` - CP: Reauth-Loop laeuft (kein TWS-Pendant).
- ``AUTH_LOST`` - CP: Reauth-Loop hat aufgegeben.
- ``CP_DOWN`` - CP: cpgateway nicht erreichbar (HTTP-Fehler beim Tickle).
- ``TWS_DOWN`` - TWS: IB-Gateway-Listener auf 4002/4001 nicht erreichbar.
- ``SESSION_LOST`` - TWS: Listener offen, aber API-Connect schlaegt fehl
  oder ``ib.client.isReady()`` ist false (typisch waehrend IBC-Restart).

Konsumenten-Mapping (siehe ``to_consumer_status``): die feinen Backend-
Werte werden auf ``ok | down | lost`` reduziert, damit Konsumenten-Code
beide Backends gleich behandeln kann.
"""
from __future__ import annotations

import enum
from typing import Literal


class AuthStatus(str, enum.Enum):
    OK = "ok"
    REAUTH_PENDING = "reauth_pending"
    AUTH_LOST = "auth_lost"
    CP_DOWN = "cp_down"
    TWS_DOWN = "tws_down"
    SESSION_LOST = "session_lost"


ConsumerAuthStatus = Literal["ok", "down", "lost"]


_CONSUMER_MAPPING: dict[AuthStatus, ConsumerAuthStatus] = {
    AuthStatus.OK: "ok",
    AuthStatus.REAUTH_PENDING: "lost",
    AuthStatus.AUTH_LOST: "lost",
    AuthStatus.SESSION_LOST: "lost",
    AuthStatus.CP_DOWN: "down",
    AuthStatus.TWS_DOWN: "down",
}


def to_consumer_status(status: AuthStatus) -> ConsumerAuthStatus:
    """Mappt einen feinen Backend-Status auf den dreistufigen
    Konsumenten-View ``ok | down | lost``.

    - ``ok`` - Session steht, Business-Calls duerfen raus.
    - ``down`` - Backend (cpgateway oder IB-Gateway) nicht erreichbar.
      Konsumenten erwarten 503 + Retry-After bei abgeleiteten Calls.
    - ``lost`` - Backend erreichbar, aber Session/Auth nicht nutzbar.
      Konsumenten sehen ebenfalls 503 + Retry-After.
    """
    return _CONSUMER_MAPPING[status]


def is_session_unavailable(status: AuthStatus) -> bool:
    """``True``, wenn Business-Endpunkte mit 503 antworten muessen.

    Spiegelt die bestehende ``cp.lifecycle.require_session_ok``-Semantik
    (CP_DOWN/AUTH_LOST → 503) und erweitert sie um die TWS-Werte
    (TWS_DOWN/SESSION_LOST → 503). REAUTH_PENDING bleibt explizit
    ausgeschlossen, weil der Reauth-Loop typischerweise innerhalb von
    Sekunden zurueck auf OK schaltet.
    """
    return status in (
        AuthStatus.CP_DOWN,
        AuthStatus.AUTH_LOST,
        AuthStatus.TWS_DOWN,
        AuthStatus.SESSION_LOST,
    )


__all__ = [
    "AuthStatus",
    "ConsumerAuthStatus",
    "is_session_unavailable",
    "to_consumer_status",
]
