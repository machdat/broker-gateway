"""GET /v1/internal/tws-health - Status des TWS-Adapters (Karte 441b53db).

Pendant zu :mod:`broker_gateway.api.v1.internal_health` (cp-basiert).
Solange der Cutover (Karte 6) noch aussteht, lebt diese Route parallel
zur cp-Route und greift auf den injektierten :class:`TWSClient` zu -
ohne den :class:`CPGatewayClient`-Pfad anzutasten.

Wird kein ``TWSClient`` ueber :func:`broker_gateway.main.create_app`
injektiert, liefert die Route ``503 tws_not_configured``. Das ist der
Default-Zustand in Production bis zum Cutover.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_ADMIN_ALL, Token
from broker_gateway.tws import TWSClient


router = APIRouter(prefix="/internal", tags=["internal-health"])


def get_tws_client() -> TWSClient:
    """Dependency-Anker fuer den TWSClient.

    Default raised ``503 tws_not_configured``. Tests und Production-
    Setups, die den Adapter aktiviert haben, ueberschreiben die
    Dependency via ``app.dependency_overrides[get_tws_client]``.
    """
    raise HTTPException(
        status_code=503,
        detail={
            "code": "tws_not_configured",
            "message": (
                "TWSClient ist nicht konfiguriert. Cutover folgt in einer "
                "spaeteren Karte; aktuell laeuft der Service ueber den "
                "CPGatewayClient-Pfad."
            ),
        },
    )


class TWSHealthResponse(BaseModel):
    connected: bool = Field(
        description="True wenn der ib_async-Socket-Connect aktiv ist"
    )
    host: str = Field(description="TWS-API-Host (Default 127.0.0.1)")
    port: int = Field(description="TWS-API-Port (Live 4001, Paper 4002)")
    paper: bool = Field(description="True wenn der Adapter den Paper-Account fuehrt")
    read_only: bool = Field(
        description="True solange Order-Routing nicht aktiviert ist"
    )
    client_id: int | None = Field(
        description=(
            "Aktuell reservierte ib_async-clientId; None wenn keine "
            "Connection besteht"
        )
    )
    checked_at: datetime = Field(description="Zeitpunkt der Auswertung (UTC)")


@router.get(
    "/tws-health",
    response_model=TWSHealthResponse,
    summary="TWS-Adapter-Health (admin:*)",
)
async def tws_health(
    _admin: Annotated[Token, Depends(require_scope(SCOPE_ADMIN_ALL))],
    client: Annotated[TWSClient, Depends(get_tws_client)],
) -> TWSHealthResponse:
    return TWSHealthResponse(
        connected=client.is_connected(),
        host=client.host,
        port=client.port,
        paper=client.paper,
        read_only=client.read_only,
        client_id=client.client_id,
        checked_at=datetime.now(UTC),
    )
