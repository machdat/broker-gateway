"""GET /v1/events/stream - SSE-Stream fuer Execution/Position/Status-Events."""
from __future__ import annotations

import secrets
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_EVENTS_READ, Token
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok
from broker_gateway.streams.events import (
    ALL_EVENT_TYPES,
    EventBus,
    get_event_bus,
)


router = APIRouter(prefix="/events", tags=["events"])


def _parse_types(raw: str | None) -> frozenset[str] | None:
    if raw is None or not raw.strip():
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    unknown = sorted(set(parts) - ALL_EVENT_TYPES)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unbekannte Event-Typen: {unknown}. Erlaubt: {sorted(ALL_EVENT_TYPES)}",
        )
    return frozenset(parts)


@router.get(
    "/stream",
    summary="SSE-Stream fuer Execution/Position/Status-Events",
    response_class=StreamingResponse,
)
async def events_stream(
    _scope: Annotated[Token, Depends(require_scope(SCOPE_EVENTS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    bus: Annotated[EventBus, Depends(get_event_bus)],
    types: Annotated[
        str | None,
        Query(description="Komma-separierte Event-Typen (execution, position, status)."),
    ] = None,
    last_event_id: Annotated[
        int | None,
        Header(alias="Last-Event-ID", description="Letzte beim Client gesehene event-id (Reconnect)"),
    ] = None,
) -> StreamingResponse:
    type_filter = _parse_types(types)
    consumer_id = secrets.token_hex(8)

    iterator = await bus.subscribe(
        consumer_id=consumer_id,
        types=type_filter,
        last_event_id=last_event_id,
    )

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


__all__ = ["router"]
