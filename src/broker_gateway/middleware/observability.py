"""Observability-Middleware: Structured-Log + Prometheus-Metric pro Request.

Setzt eine `request_id` (UUID) an den Response-Header `X-Request-ID` und
emittiert nach jedem Request ein JSON-Log-Event mit Pflichtfeldern. Der
Token-Wert taucht NIE im Log auf - nur `caller_id` und `scopes`. Die
Auth-Dependency hinterlegt das Token-Objekt in `request.state.auth_token`,
damit die Middleware nach Routing-Ende dort drauf zugreifen kann.
"""
from __future__ import annotations

import time
import uuid
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from broker_gateway.logging_setup import get_logger
from broker_gateway.metrics import BrokerGatewayMetrics


_log = get_logger("broker_gateway.http")


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Eine Middleware fuer Logging + Metrics pro HTTP-Request."""

    def __init__(self, app, *, metrics: BrokerGatewayMetrics) -> None:
        super().__init__(app)
        self._metrics = metrics

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = request_id
        path_template = _path_template(request)
        start = time.monotonic()

        try:
            response = await call_next(request)
            status_code = response.status_code
        except Exception:
            elapsed = time.monotonic() - start
            self._metrics.requests_total.labels(
                path=path_template, status="500", scope="-"
            ).inc()
            self._metrics.request_latency_seconds.labels(path=path_template).observe(elapsed)
            _log.error(
                "http_request_failed",
                request_id=request_id,
                method=request.method,
                path=path_template,
                latency_ms=round(elapsed * 1000.0, 2),
                exc_info=True,
            )
            raise

        elapsed = time.monotonic() - start
        token = getattr(request.state, "auth_token", None)
        caller_id = token.caller_id if token is not None else None
        scope_list = list(token.scopes) if token is not None else []
        idem_key = request.headers.get("Idempotency-Key")

        self._metrics.requests_total.labels(
            path=path_template,
            status=str(status_code),
            scope=",".join(scope_list) or "-",
        ).inc()
        self._metrics.request_latency_seconds.labels(path=path_template).observe(elapsed)

        _log.info(
            "http_request",
            request_id=request_id,
            method=request.method,
            path=path_template,
            status=status_code,
            latency_ms=round(elapsed * 1000.0, 2),
            caller_id=caller_id,
            scopes=scope_list,
            idempotency_key=idem_key,
        )

        response.headers["X-Request-ID"] = request_id
        return response


def _path_template(request: Request) -> str:
    """Liefert das Routen-Template (z.B. /v1/orders/{order_id}) statt
    eines konkreten Pfades. Verhindert High-Cardinality im Prometheus-
    Counter durch unique-IDs in der path-Label.
    """
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None)
    if isinstance(path_format, str) and path_format:
        return path_format
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return request.url.path


__all__ = ["ObservabilityMiddleware"]
