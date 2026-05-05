"""GET /v1/instruments/search und /v1/instruments/{conid}."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

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
    summary="Symbol- oder ISIN-Lookup (TTL-Cache 7 Tage)",
)
async def search_instruments(
    _scope: Annotated[Token, Depends(require_scope(SCOPE_INSTRUMENTS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[InstrumentsService, Depends(get_instruments_service)],
    symbol: Annotated[
        str | None, Query(min_length=1, description="z.B. AAPL")
    ] = None,
    exchange: Annotated[
        str | None, Query(description="z.B. NASDAQ - nur in Kombination mit symbol")
    ] = None,
    isin: Annotated[
        str | None,
        Query(
            min_length=12,
            max_length=12,
            description="ISO 6166, z.B. DE0007164600 (SAP)",
        ),
    ] = None,
    mic: Annotated[
        str | None,
        Query(
            description=(
                "ISO 10383 MIC zur Cross-Listing-Disambiguation, "
                "z.B. XETR oder XNYS - nur in Kombination mit isin"
            ),
        ),
    ] = None,
) -> list[Instrument]:
    if symbol is None and isin is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="symbol oder isin muss angegeben werden",
        )
    if symbol is not None and isin is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="symbol und isin schliessen sich aus - genau einer der beiden",
        )
    if isin is not None:
        if exchange is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="exchange ist nur in Kombination mit symbol erlaubt",
            )
        return await service.search_by_isin(isin, mic)
    if mic is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="mic ist nur in Kombination mit isin erlaubt",
        )
    assert symbol is not None  # narrowing
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
