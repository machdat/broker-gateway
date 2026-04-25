"""GET /v1/instruments/search und /v1/instruments/{conid}."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_INSTRUMENTS_READ, Token
from broker_gateway.cp.instruments import (
    Instrument,
    InstrumentDetail,
    InstrumentsService,
)
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok


router = APIRouter(prefix="/instruments", tags=["instruments"])


def get_instruments_service() -> InstrumentsService:
    raise RuntimeError(
        "get_instruments_service muss in der App per dependency_overrides gesetzt werden"
    )


@router.get(
    "/search",
    response_model=list[Instrument],
    summary="Symbol-Lookup (TTL-Cache 7 Tage)",
)
async def search_instruments(
    symbol: Annotated[str, Query(min_length=1, description="z.B. AAPL")],
    _scope: Annotated[Token, Depends(require_scope(SCOPE_INSTRUMENTS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[InstrumentsService, Depends(get_instruments_service)],
    exchange: Annotated[str | None, Query(description="z.B. NASDAQ")] = None,
) -> list[Instrument]:
    return await service.search(symbol, exchange)


@router.get(
    "/{conid}",
    response_model=InstrumentDetail,
    summary="Instrument-Detail per conid",
)
async def get_instrument(
    conid: int,
    _scope: Annotated[Token, Depends(require_scope(SCOPE_INSTRUMENTS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[InstrumentsService, Depends(get_instruments_service)],
) -> InstrumentDetail:
    return await service.info(conid)
