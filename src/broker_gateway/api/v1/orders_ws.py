"""GET /v1/orders/ws - WebSocket-Egress fuer den Order-Stream (AP-11 K7).

Vertrag identisch zum SSE-Pfad ``/v1/orders/stream``.
"""
from __future__ import annotations

import json
import secrets

from fastapi import APIRouter, Depends, Query, WebSocket
from starlette.websockets import WebSocketDisconnect

from broker_gateway.api.v1.orders_stream import (
    OrdersBootstrapLoader,
    get_orders_bootstrap_loader,
)
from broker_gateway.api.v1.ws_auth import (
    WebSocketAuthError,
    authenticate_websocket,
)
from broker_gateway.auth.middleware import get_token_store
from broker_gateway.auth.models import SCOPE_ORDERS_READ, SCOPE_ORDERS_WRITE
from broker_gateway.auth.store import TokenStore
from broker_gateway.cp.lifecycle import AuthLifecycle, get_cp_lifecycle
from broker_gateway.streams.orders import (
    OrdersBroadcaster,
    get_orders_broadcaster,
)


router = APIRouter(prefix="/orders", tags=["orders-ws"])


@router.websocket("/ws")
async def orders_ws(
    websocket: WebSocket,
    account: str = Query(..., min_length=1),
    last_event_id: int | None = Query(None),
    store: TokenStore = Depends(get_token_store),
    broadcaster: OrdersBroadcaster = Depends(get_orders_broadcaster),
    loader: OrdersBootstrapLoader = Depends(get_orders_bootstrap_loader),
    lifecycle: AuthLifecycle = Depends(get_cp_lifecycle),
) -> None:
    try:
        # Scope-Semantik wie GET /v1/orders/{order_id}: orders:write
        # schliesst das Leserecht mit ein (Karte 601c6e09).
        await authenticate_websocket(
            websocket, store, SCOPE_ORDERS_READ, SCOPE_ORDERS_WRITE
        )
    except WebSocketAuthError:
        return

    consumer_id = secrets.token_hex(8)
    try:
        bootstrap = await loader.load(account)
    except Exception:
        await websocket.close(code=1011)
        return

    iterator = await broadcaster.subscribe(
        account=account,
        consumer_id=consumer_id,
        bootstrap=bootstrap,
        last_event_id=last_event_id,
    )

    try:
        async for event in iterator:
            await websocket.send_text(
                json.dumps(
                    {
                        "id": event.event_id,
                        "event": event.event_type,
                        "data": event.payload,
                    },
                    separators=(",", ":"),
                    default=str,
                )
            )
    except WebSocketDisconnect:
        return
    except Exception:
        try:
            await websocket.close(code=1011)
        except Exception:
            pass


__all__ = ["router", "orders_ws"]
