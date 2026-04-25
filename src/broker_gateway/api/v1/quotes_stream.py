"""GET /v1/quotes/stream - SSE-Stream mit Subscription-Refcount + Fan-Out."""
from __future__ import annotations

import secrets
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_QUOTES_READ, Token
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok
from broker_gateway.cp.quotes import normalize_default_fields, resolve_fields
from broker_gateway.streams.manager import (
    StreamEvent,
    SubscriptionLimitExceeded,
    SubscriptionManager,
    get_subscription_manager,
)


router = APIRouter(prefix="/quotes", tags=["quotes-stream"])

_RETRY_AFTER_S = 30


def _parse_conids(raw: str) -> list[int]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="conids darf nicht leer sein",
        )
    try:
        return [int(p) for p in parts]
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"conids muss Komma-separierte Integer enthalten: {exc}",
        ) from exc


@router.get(
    "/stream",
    summary="SSE-Stream fuer Marktdaten (Refcounted, Fan-Out)",
    response_class=StreamingResponse,
)
async def quotes_stream(
    conids: Annotated[str, Query(min_length=1, description="Komma-separierte conid-Liste")],
    _scope: Annotated[Token, Depends(require_scope(SCOPE_QUOTES_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    manager: Annotated[SubscriptionManager, Depends(get_subscription_manager)],
    fields: Annotated[
        str | None,
        Query(description="Komma-separierte Feldnamen (analog Snapshot-Endpoint)"),
    ] = None,
    last_event_id: Annotated[
        int | None,
        Header(alias="Last-Event-ID", description="Letzte beim Client gesehene event-id (Reconnect)"),
    ] = None,
) -> StreamingResponse:
    parsed_conids = _parse_conids(conids)
    field_names = (
        [f.strip() for f in fields.split(",") if f.strip()]
        if fields
        else list(normalize_default_fields())
    )
    resolved = resolve_fields(field_names)

    consumer_id = secrets.token_hex(8)

    try:
        iterator = await manager.subscribe(
            conids=parsed_conids,
            field_codes=resolved,
            consumer_id=consumer_id,
            last_event_id=last_event_id,
        )
    except SubscriptionLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=str(exc),
            headers={"Retry-After": str(_RETRY_AFTER_S)},
        ) from exc

    async def _to_sse() -> AsyncIterator[bytes]:
        async for event in iterator:
            yield event.to_sse_payload().encode("utf-8")

    return StreamingResponse(
        _to_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["router", "StreamEvent"]
