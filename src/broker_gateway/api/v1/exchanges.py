"""GET /v1/exchanges und /v1/exchanges/{exchange_id}/calendar.

Liefert die Daten aus einem CalendarService-Adapter (cp oder tws). Der
Endpoint ist backend-agnostisch und nutzt die gemeinsame Public-API
``cached_exchanges`` / ``time_zone_for`` / ``description_for`` / ``get``,
die sowohl ``cp.calendar.CalendarService`` als auch
``tws.calendar.TWSCalendarService`` implementieren.

AP ``2a203c58-...`` Phase 5: Bei leerem ``cached_exchanges`` antwortet
``GET /v1/exchanges`` mit HTTP 503 + ``calendar_unavailable`` statt
HTTP 200 + leerem Array. Damit endet die stille Regression im cp-Pfad,
die bei DNS-Fehler auf cpgateway zu einem getarnten Defekt fuehrte.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, Field

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_INSTRUMENTS_READ, Token
from broker_gateway.cp.calendar import (
    CalendarDay,
    CalendarService,
    ExchangeCalendar,
)
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok


router = APIRouter(prefix="/exchanges", tags=["exchanges"])


_MAX_DAYS = 14


class ExchangeListEntry(BaseModel):
    exchange_id: str
    description: str | None = None
    time_zone: str | None = Field(
        default=None,
        description="IANA-Zone aus dem Calendar-Backend",
    )


class ExchangeListResponse(BaseModel):
    exchanges: list[ExchangeListEntry]
    cached_calendars: int


def get_calendar_service() -> CalendarService:
    raise RuntimeError(
        "get_calendar_service muss in der App per dependency_overrides "
        "gesetzt werden"
    )


@router.get(
    "",
    response_model=ExchangeListResponse,
    summary="Liste der bekannten Boersen",
)
async def list_exchanges(
    _scope: Annotated[Token, Depends(require_scope(SCOPE_INSTRUMENTS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[CalendarService, Depends(get_calendar_service)],
) -> ExchangeListResponse:
    cached = service.cached_exchanges
    if not cached:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "calendar_unavailable",
                "message": (
                    "Keine Boersenkalender verfuegbar - das Calendar-"
                    "Backend hat keine Daten geladen."
                ),
            },
        )
    entries = [
        ExchangeListEntry(
            exchange_id=exchange_id,
            description=service.description_for(exchange_id),
            time_zone=service.time_zone_for(exchange_id),
        )
        for exchange_id in cached
    ]
    return ExchangeListResponse(
        exchanges=entries,
        cached_calendars=len(entries),
    )


@router.get(
    "/{exchange_id}/calendar",
    response_model=ExchangeCalendar,
    summary="Boersenkalender (1 bis 14 Tage)",
)
async def get_calendar(
    exchange_id: Annotated[
        str,
        Path(min_length=1, description="z.B. NASDAQ"),
    ],
    _scope: Annotated[Token, Depends(require_scope(SCOPE_INSTRUMENTS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[CalendarService, Depends(get_calendar_service)],
    days: Annotated[
        int,
        Query(
            ge=1,
            le=_MAX_DAYS,
            description="Anzahl Tage (Default 14, Range 1..14)",
        ),
    ] = _MAX_DAYS,
) -> ExchangeCalendar:
    calendar = await service.get(exchange_id)
    truncated_days: list[CalendarDay] = list(calendar.days[:days])
    return ExchangeCalendar(
        exchange_id=calendar.exchange_id,
        time_zone=calendar.time_zone,
        days=truncated_days,
    )


__all__ = [
    "ExchangeListEntry",
    "ExchangeListResponse",
    "get_calendar_service",
    "router",
]
