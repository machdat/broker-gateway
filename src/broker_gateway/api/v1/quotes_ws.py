"""GET /v1/quotes/ws - WebSocket-Egress fuer den Quote-Stream (AP-11 K7).

Vertrag identisch zum SSE-Pfad ``/v1/quotes/stream``: gleicher
``StreamHub`` als Quelle, gleiches Frame-Schema (Anhang B), Last-Event-
ID-Replay-Param. Frames werden als Text-Frames mit JSON-Body gesendet,
in einem Wrapper ``{id, event, data}``, sodass Browser-Clients ohne
SSE-Parsing dieselben Daten konsumieren koennen.
"""
from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Depends, Query, WebSocket
from starlette.websockets import WebSocketDisconnect

from broker_gateway.api.v1.ws_auth import (
    WebSocketAuthError,
    authenticate_websocket,
)
from broker_gateway.auth.middleware import get_token_store
from broker_gateway.auth.models import SCOPE_QUOTES_READ
from broker_gateway.auth.store import TokenStore
from broker_gateway.cp.lifecycle import AuthLifecycle, get_cp_lifecycle
from broker_gateway.cp.quotes import normalize_default_fields, resolve_fields
from broker_gateway.streams.manager import (
    SubscriptionManager,
    get_subscription_manager,
)


router = APIRouter(prefix="/quotes", tags=["quotes-ws"])


@router.websocket("/ws")
async def quotes_ws(
    websocket: WebSocket,
    conids: str = Query(..., min_length=1),
    fields: str | None = Query(None),
    last_event_id: int | None = Query(None),
    store: TokenStore = Depends(get_token_store),
    manager: SubscriptionManager = Depends(get_subscription_manager),
    lifecycle: AuthLifecycle = Depends(get_cp_lifecycle),
) -> None:
    try:
        await authenticate_websocket(websocket, store, SCOPE_QUOTES_READ)
    except WebSocketAuthError:
        return

    parsed_conids = _parse_conids(conids)
    if parsed_conids is None:
        await websocket.close(code=1003)  # unsupported data
        return

    field_names = (
        [f.strip() for f in fields.split(",") if f.strip()]
        if fields
        else list(normalize_default_fields())
    )
    try:
        resolved = resolve_fields(field_names)
    except Exception:
        await websocket.close(code=1003)
        return

    consumer_id = secrets.token_hex(8)
    iterator = await manager.subscribe(
        conids=parsed_conids,
        field_codes=resolved,
        consumer_id=consumer_id,
        last_event_id=last_event_id,
    )

    try:
        async for event in iterator:
            await websocket.send_text(
                json.dumps(
                    {
                        "id": event.event_id,
                        "event": event.event_type,
                        "data": event.quote.model_dump(mode="json"),
                    },
                    separators=(",", ":"),
                    default=str,
                )
            )
    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await websocket.close(code=1011)  # internal error
        except Exception:
            pass


def _parse_conids(raw: str) -> list[int] | None:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    try:
        return [int(p) for p in parts]
    except ValueError:
        return None


__all__ = ["router", "quotes_ws"]
