"""GET /v1/trades und /v1/trades/aggregates.

Trades-Liste und Aggregat-Endpunkt fuer KESt-/FIFO-Berechnungen im PSM.
Aggregation laeuft ausschliesslich serverseitig (nicht beim Consumer),
damit derselbe Code-Pfad fuer trading-robot und PSM gilt.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_PORTFOLIO_READ, Token
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok
from broker_gateway.cp.trades import Trade, TradesAggregate, TradesService


router = APIRouter(prefix="/trades", tags=["trades"])


_DEFAULT_LOOKBACK_DAYS = 7
_MAX_LOOKBACK_DAYS = 30


class TradesList(BaseModel):
    items: list[Trade]
    period_from: date
    period_to: date
    trade_count: int


def get_trades_service() -> TradesService:
    raise RuntimeError(
        "get_trades_service muss in der App per dependency_overrides gesetzt werden"
    )


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _resolve_window(period_from: date | None, period_to: date | None) -> tuple[date, date]:
    today = _today_utc()
    end = period_to or today
    start = period_from or end.fromordinal(end.toordinal() - _DEFAULT_LOOKBACK_DAYS + 1)
    if end < start:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`to` muss >= `from` sein",
        )
    if (today.toordinal() - start.toordinal()) > _MAX_LOOKBACK_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Lookback maximal {_MAX_LOOKBACK_DAYS} Tage (CP-Gateway-Limit)",
        )
    return start, end


@router.get(
    "",
    response_model=TradesList,
    summary="Trade-Historie (Ausfuehrungen)",
)
async def list_trades(
    _scope: Annotated[Token, Depends(require_scope(SCOPE_PORTFOLIO_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[TradesService, Depends(get_trades_service)],
    account_id: Annotated[str | None, Query()] = None,
    period_from: Annotated[date | None, Query(alias="from")] = None,
    period_to: Annotated[date | None, Query(alias="to")] = None,
) -> TradesList:
    start, end = _resolve_window(period_from, period_to)
    today = _today_utc()
    trades = await service.list_trades(
        period_from=start,
        period_to=end,
        account_id=account_id,
        today=today,
    )
    return TradesList(
        items=trades,
        period_from=start,
        period_to=end,
        trade_count=len(trades),
    )


@router.get(
    "/aggregates",
    response_model=TradesAggregate,
    summary="Trade-Aggregate (z.B. commissions_mtd)",
)
async def trade_aggregates(
    _scope: Annotated[Token, Depends(require_scope(SCOPE_PORTFOLIO_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[TradesService, Depends(get_trades_service)],
    metric: Annotated[Literal["commissions_mtd"], Query()] = "commissions_mtd",
    account_id: Annotated[str | None, Query()] = None,
) -> TradesAggregate:
    if metric == "commissions_mtd":
        return await service.commissions_mtd(account_id=account_id, today=_today_utc())
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unbekannte metric: {metric}",
    )
