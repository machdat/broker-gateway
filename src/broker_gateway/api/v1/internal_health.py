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
    last_sso_validate_at: datetime | None = Field(
        default=None,
        description="Zeitpunkt des letzten GET /sso/validate (primaerer Keep-Alive)",
    )
    last_login_at: datetime | None = Field(
        default=None,
        description="Zeitpunkt des letzten Uebergangs in den OK-Zustand "
        "(Initial-Login oder erfolgreicher Reauth)",
    )
    session_age_s: float | None = Field(description="Sekunden seit letztem OK-Tickle")
    consecutive_reauth_failures: int
    accounts_initialized: bool = Field(
        default=False,
        description="True, sobald GET /iserver/accounts nach Login einmal "
        "erfolgreich ausgeloest wurde",
    )
    iserver_bridge_ok: bool | None = Field(
        default=None,
        description="Letztes Resultat der iserver-Bridge-Probe via "
        "GET /iserver/auth/status: True wenn das Trio "
        "(authenticated/established/connected) gesund war, False bei "
        "Bridge-Drift, None solange noch kein Probe gelaufen ist",
    )
    last_bridge_probe_at: datetime | None = Field(
        default=None,
        description="Zeitpunkt des letzten iserver-Bridge-Probes",
    )
    consecutive_bridge_failures: int = Field(
        default=0,
        description="Aufeinanderfolgende Bridge-Drift-Befunde; reset auf 0 "
        "nach erfolgreichem Probe oder erfolgreicher Recovery",
    )


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
        last_sso_validate_at=snap.last_sso_validate_at,
        last_login_at=snap.last_login_at,
        session_age_s=snap.session_age_s,
        consecutive_reauth_failures=snap.consecutive_reauth_failures,
        accounts_initialized=snap.accounts_initialized,
        iserver_bridge_ok=snap.iserver_bridge_ok,
        last_bridge_probe_at=snap.last_bridge_probe_at,
        consecutive_bridge_failures=snap.consecutive_bridge_failures,
    )
