"""GET /v1/exchanges und /v1/exchanges/{exchange_id}/calendar.

Liefert die Daten aus dem :class:`broker_gateway.cp.calendar.CalendarService`
gemaess K6-Sektion 5.3 / Anhang C.4.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
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
        description="IANA-Zone aus dem zuletzt gefetchten Schedule",
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
    summary="Liste der bisher gesehenen Boersen (aus dem Schedule-Cache)",
)
async def list_exchanges(
    _scope: Annotated[Token, Depends(require_scope(SCOPE_INSTRUMENTS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[CalendarService, Depends(get_calendar_service)],
) -> ExchangeListResponse:
    cached = service.cached_exchanges
    entries: list[ExchangeListEntry] = []
    for exchange_id in cached:
        entry_cache = service._cache.get(exchange_id)  # noqa: SLF001
        time_zone = (
            entry_cache.calendar.time_zone if entry_cache is not None else None
        )
        entries.append(
            ExchangeListEntry(
                exchange_id=exchange_id,
                description=None,
                time_zone=time_zone,
            )
        )
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
