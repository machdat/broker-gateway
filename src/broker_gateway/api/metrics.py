"""GET /metrics - Prometheus-Scrape-Endpunkt.

Bewusst nicht unter `/v1`: Metriken sind eine Service-Schnittstelle, kein
Consumer-API-Vertrag. Im Compose-Setup wird der Port nur intern
publiziert. Kein Auth-Scope, dafuer Reverse-Proxy-Allowlist (TODO im
Deployment).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST

from broker_gateway.metrics import BrokerGatewayMetrics, get_metrics


router = APIRouter()


@router.get(
    "/metrics",
    summary="Prometheus-Scrape-Endpoint",
    response_class=Response,
)
async def metrics(
    metrics_obj: Annotated[BrokerGatewayMetrics, Depends(get_metrics)],
) -> Response:
    return Response(content=metrics_obj.render(), media_type=CONTENT_TYPE_LATEST)


__all__ = ["router"]
