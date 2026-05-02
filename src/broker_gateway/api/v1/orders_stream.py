"""GET /v1/orders/stream - SSE-Stream fuer Live-Order-Updates (AP-11 K6).

Erster Frame ist der REST-Bootstrap (alle aktuell offenen Orders),
danach folgen Live-sor-Updates aus dem ``OrdersBroadcaster``. Vertrag
analog zu ``/v1/quotes/stream``: Last-Event-ID-Reconnect, 15-s-
Heartbeat als SSE-Comment, semantisches JSON pro Frame.
"""
from __future__ import annotations

import json
import secrets
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_ORDERS_READ, Token
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok
from broker_gateway.cp.topics.sor import SorTopicAdapter
from broker_gateway.streams.orders import (
    OrdersBroadcaster,
    get_orders_broadcaster,
)


router = APIRouter(prefix="/orders", tags=["orders-stream"])


def get_orders_bootstrap_loader():
    raise RuntimeError(
        "get_orders_bootstrap_loader muss per dependency_overrides "
        "gesetzt werden"
    )


@router.get(
    "/stream",
    summary="SSE-Stream fuer Live-Order-Updates (Bootstrap + Live)",
    response_class=StreamingResponse,
)
async def orders_stream(
    account: Annotated[str, Query(min_length=1, description="Konto-Nummer")],
    _scope: Annotated[Token, Depends(require_scope(SCOPE_ORDERS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    broadcaster: Annotated[
        OrdersBroadcaster, Depends(get_orders_broadcaster)
    ],
    loader: Annotated[
        "OrdersBootstrapLoader", Depends(get_orders_bootstrap_loader)
    ],
    last_event_id: Annotated[
        int | None,
        Header(
            alias="Last-Event-ID",
            description="Letzte gesehene event-id (Reconnect)",
        ),
    ] = None,
) -> StreamingResponse:
    consumer_id = secrets.token_hex(8)
    bootstrap = await loader.load(account)
    iterator = await broadcaster.subscribe(
        account=account,
        consumer_id=consumer_id,
        bootstrap=bootstrap,
        last_event_id=last_event_id,
    )

    async def _to_sse() -> AsyncIterator[bytes]:
        async for event in iterator:
            yield (
                f"id: {event.event_id}\n"
                f"event: {event.event_type}\n"
                f"data: {json.dumps(event.payload, separators=(',', ':'))}\n\n"
            ).encode("utf-8")

    return StreamingResponse(
        _to_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


class OrdersBootstrapLoader:
    """Laedt offene Orders einmal beim Subscribe-Start.

    Ruft ``GET /iserver/account/orders`` und reicht das Ergebnis durch
    den ``SorTopicAdapter.bootstrap``-Pfad, sodass die initialen
    Snapshots dieselbe semantische Form haben wie Live-Frames.
    """

    def __init__(
        self,
        client: CPGatewayClient,
        adapter: SorTopicAdapter,
    ) -> None:
        self._client = client
        self._adapter = adapter

    async def load(self, account: str):
        response = await self._client.get(
            "/iserver/account/orders",
            params={"accountId": account},
        )
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=(
                    "CP-Gateway-Fehler bei iserver/account/orders: "
                    f"HTTP {response.status_code}"
                ),
            )
        payload = response.json()
        if isinstance(payload, dict):
            orders = payload.get("orders") or []
        elif isinstance(payload, list):
            orders = payload
        else:
            orders = []
        return self._adapter.bootstrap(orders)


__all__ = [
    "OrdersBootstrapLoader",
    "get_orders_bootstrap_loader",
    "orders_stream",
    "router",
]
