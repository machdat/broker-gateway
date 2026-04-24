"""GET /v1/internal/health - Detail-Health für Operations / oncall.

Geschützt mit `admin:*`-Scope. Liefert immer 200, auch wenn die
IBKR-Session verloren ist - das ist ja gerade das, was man erfahren
will. Business-Endpunkte signalisieren Session-Verlust separat per
503 + Retry-After.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.cp.lifecycle import AuthLifecycle, AuthStatus, get_cp_lifecycle


router = APIRouter(prefix="/internal", tags=["internal-health"])


class InternalHealthResponse(BaseModel):
    auth_status: AuthStatus = Field(description="Aktueller IBKR-Session-Zustand")
    cp_reachable: bool = Field(description="Letzter HTTP-Call zum CP-Gateway hat geantwortet")
    last_tickle_at: datetime | None
    last_reauth_at: datetime | None
    session_age_s: float | None = Field(description="Sekunden seit letztem OK-Tickle")
    consecutive_reauth_failures: int


@router.get("/health", response_model=InternalHealthResponse, summary="Detail-Health (admin:*)")
def internal_health(
    _admin: Annotated[Token, Depends(require_scope(SCOPE_ADMIN_ALL))],
    lifecycle: Annotated[AuthLifecycle, Depends(get_cp_lifecycle)],
) -> InternalHealthResponse:
    snap = lifecycle.snapshot()
    return InternalHealthResponse(
        auth_status=snap.auth_status,
        cp_reachable=snap.cp_reachable,
        last_tickle_at=snap.last_tickle_at,
        last_reauth_at=snap.last_reauth_at,
        session_age_s=snap.session_age_s,
        consecutive_reauth_failures=snap.consecutive_reauth_failures,
    )
