"""GET /v1/portfolio/{accountId}, /positions, /ledger."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_PORTFOLIO_READ, Token
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok
from broker_gateway.cp.portfolio import (
    Ledger,
    PortfolioService,
    PortfolioSummary,
    Position,
)


router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def get_portfolio_service() -> PortfolioService:
    raise RuntimeError(
        "get_portfolio_service muss in der App per dependency_overrides gesetzt werden"
    )


def _validate_account_id(account_id: str) -> str:
    cleaned = account_id.strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="account_id darf nicht leer sein",
        )
    return cleaned


@router.get(
    "/{account_id}",
    response_model=PortfolioSummary,
    summary="Portfolio-Summary (cash + positions_value + net_liquidation)",
)
async def portfolio_summary(
    account_id: Annotated[str, Path(min_length=1)],
    _scope: Annotated[Token, Depends(require_scope(SCOPE_PORTFOLIO_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[PortfolioService, Depends(get_portfolio_service)],
) -> PortfolioSummary:
    return await service.summary(_validate_account_id(account_id))


@router.get(
    "/{account_id}/positions",
    response_model=list[Position],
    summary="Aktuelle Holdings",
)
async def portfolio_positions(
    account_id: Annotated[str, Path(min_length=1)],
    _scope: Annotated[Token, Depends(require_scope(SCOPE_PORTFOLIO_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[PortfolioService, Depends(get_portfolio_service)],
) -> list[Position]:
    return await service.positions(_validate_account_id(account_id))


@router.get(
    "/{account_id}/ledger",
    response_model=Ledger,
    summary="Cash-Ledger pro Currency",
)
async def portfolio_ledger(
    account_id: Annotated[str, Path(min_length=1)],
    _scope: Annotated[Token, Depends(require_scope(SCOPE_PORTFOLIO_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[PortfolioService, Depends(get_portfolio_service)],
) -> Ledger:
    return await service.ledger(_validate_account_id(account_id))
