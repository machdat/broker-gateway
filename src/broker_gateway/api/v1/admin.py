"""Admin-Endpoints (admin:*-Scope only).

Enthaelt heute nur ``POST /v1/admin/auto-login/trigger``. Weitere
Admin-Routen koennen hier eingehaengt werden, wenn sie eindeutig
operationelle Aktionen sind und kein Geschaeftsdaten-Konzept haben.
"""
from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token


logger = logging.getLogger(__name__)


router = APIRouter(prefix="/admin", tags=["admin"])


class AutoLoginTriggerResponse(BaseModel):
    """Antwort des manuellen Trigger-Aufrufs.

    ``skipped=True`` heisst eine Pre-Condition hat den Lauf
    verhindert (z.B. Throttle, Stack-Kind, Auth-Status), und
    ``reason`` traegt den Grund. Bei ``skipped=False`` ist
    ``result`` mit dem Sidecar-Resultat gefuellt.
    """

    skipped: bool
    reason: str
    exit_code: int | None = None
    duration_s: float | None = None
    error: str | None = None


@router.post(
    "/auto-login/trigger",
    response_model=AutoLoginTriggerResponse,
    summary="Auto-Login-Sidecar manuell anstossen (admin:*)",
)
async def trigger_auto_login(
    request: Request,
    _admin: Annotated[Token, Depends(require_scope(SCOPE_ADMIN_ALL))],
) -> AutoLoginTriggerResponse:
    """Anstoss fuer einen einmaligen Auto-Login-Versuch.

    Respektiert dieselben Hard-Guards und Throttle-Limits wie der
    automatische Pfad — Operator-Aufrufe brennen also nicht das
    Tageslimit unbemerkt durch.
    """
    trigger = getattr(request.app.state, "auto_login_trigger", None)
    if trigger is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "auto_login_disabled",
                "message": (
                    "Auto-Login ist in diesem Stack nicht aktiviert "
                    "(BG_PAPER_AUTO_LOGIN=0 oder BG_STACK_KIND!=paper)."
                ),
            },
        )
    outcome = await trigger.maybe_trigger()
    result = outcome.result
    return AutoLoginTriggerResponse(
        skipped=outcome.skipped,
        reason=outcome.reason,
        exit_code=result.exit_code if result is not None else None,
        duration_s=result.duration_s if result is not None else None,
        error=result.error if result is not None else None,
    )
