"""WS-Auth-Helper fuer die Egress-Endpunkte (AP-11 K7).

Akzeptiert Bearer-Token aus drei Quellen:

1. ``Authorization: Bearer <token>`` (REST-Standard, fuer Tools wie wscat).
2. ``Sec-WebSocket-Protocol: bearer.<token>`` (Browser-Pattern; das
   Echo des akzeptierten Subprotokolls ist Pflicht).
3. ``?token=<token>``-Query-Param (Fallback fuer Browser-Clients ohne
   Subprotokoll-Kontrolle).

Bei fehlender / ungueltiger Auth wird der WebSocket mit Close-Code
1008 (Policy Violation) geschlossen, ohne Body. Der Caller darf nach
``authenticate_websocket`` direkt mit ``send_text`` weitermachen.
"""
from __future__ import annotations

from typing import Tuple

from fastapi import WebSocket, status
from starlette.websockets import WebSocketDisconnect

from broker_gateway.auth.models import Token
from broker_gateway.auth.store import TokenStore


_BEARER_PROTOCOL_PREFIX = "bearer."


class WebSocketAuthError(Exception):
    """Auth fehlgeschlagen - der Caller schliesst den Socket bereits."""


async def authenticate_websocket(
    websocket: WebSocket,
    store: TokenStore,
    *required_scopes: str,
) -> Token:
    """Akzeptiert die WS-Verbindung und prueft Token + Scope.

    ``required_scopes`` hat OR-Semantik: der Token muss MINDESTENS
    einen davon besitzen. Das ist das WS-Gegenstück zu
    :func:`broker_gateway.auth.middleware.require_any_scope` und aus
    demselben Grund so - eine Schreibberechtigung schließt das
    Leserecht auf dieselbe Ressource mit ein, ein Write-only-Token
    darf am Lese-Endpunkt also nicht brechen (Karte ``601c6e09``).

    Aufrufer mit genau einem Scope bleiben unverändert gültig.

    Bei Erfolg ist die Verbindung offen und der Token wird zurueck-
    gegeben. Bei Fehler ist die Verbindung geschlossen und es wird
    ``WebSocketAuthError`` geworfen.
    """
    if not required_scopes:
        raise ValueError("authenticate_websocket braucht mindestens einen Scope")
    raw_token, subprotocol = _extract_token(websocket)
    if not raw_token:
        await _close(websocket, code=status.WS_1008_POLICY_VIOLATION)
        raise WebSocketAuthError("kein Token mitgeliefert")

    token = store.get(raw_token)
    if token is None or token.is_expired():
        await _close(websocket, code=status.WS_1008_POLICY_VIOLATION)
        raise WebSocketAuthError("Token unbekannt oder abgelaufen")

    if not any(token.has_scope(scope) for scope in required_scopes):
        await _close(websocket, code=status.WS_1008_POLICY_VIOLATION)
        raise WebSocketAuthError(
            "Token besitzt keinen der erforderlichen Scopes: "
            + ", ".join(repr(scope) for scope in required_scopes)
        )

    # Subprotocol echoen, wenn Auth via Sec-WebSocket-Protocol kam -
    # Browser-Clients schliessen sonst den Socket clientseitig wieder.
    if subprotocol is not None:
        await websocket.accept(subprotocol=subprotocol)
    else:
        await websocket.accept()
    return token


def _extract_token(websocket: WebSocket) -> Tuple[str | None, str | None]:
    # 1. Authorization-Header (REST-Standard).
    auth_header = websocket.headers.get("authorization")
    if auth_header:
        parts = auth_header.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            return parts[1].strip(), None

    # 2. Sec-WebSocket-Protocol: ``bearer.<token>``.
    proto_header = websocket.headers.get("sec-websocket-protocol")
    if proto_header:
        for proto in (p.strip() for p in proto_header.split(",")):
            if proto.startswith(_BEARER_PROTOCOL_PREFIX):
                token = proto[len(_BEARER_PROTOCOL_PREFIX):]
                if token:
                    return token, proto

    # 3. Query-Param ``?token=...``.
    query_token = websocket.query_params.get("token")
    if query_token and query_token.strip():
        return query_token.strip(), None

    return None, None


async def _close(websocket: WebSocket, *, code: int) -> None:
    try:
        await websocket.close(code=code)
    except (RuntimeError, WebSocketDisconnect):
        # Verbindung bereits zu - ignorieren.
        pass


__all__ = ["WebSocketAuthError", "authenticate_websocket"]
