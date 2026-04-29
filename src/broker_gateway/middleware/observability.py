"""Observability-Middleware: structured Log + Prometheus-Metric pro Request.

Setzt eine ``request_id`` (UUID) am Response-Header ``X-Request-ID`` und
emittiert nach jedem Request ein JSON-Log-Event mit Metadaten plus -
sofern ``BG_LOG_INBOUND_BODIES=on`` (Default) - Request- und Response-
Headern (gefiltert via :mod:`broker_gateway.cp.redaction`) und Bodies.
Token-Werte tauchen NIE im Log auf.

Stream-Replacement-Pattern: ``request.body()`` wird VOR ``call_next``
einmal gelesen; ``request._receive`` wird durch ein Replay ersetzt, das
denselben Body als einzelnen Chunk zurueckspielt - so kann der
nachgelagerte Endpunkt ``await request.body()``, ``request.json()``,
``request.form()`` weiterhin nutzen. Response-Body wird durch Sammeln
des ``body_iterator`` materialisiert und in einen neuen ``Response``
verpackt; SSE-Antworten (``text/event-stream``) bleiben unangetastet
und werden mit ``response_streaming=true`` markiert.

structlog-ContextVars werden mit ``request_id`` befuellt, damit
nachgelagerte Events (z.B. der kommende CP-Wire-Hook) die Korrelations-
ID automatisch in jedem Event tragen.
"""
from __future__ import annotations

import base64
import json
import os
import time
import uuid
from typing import Any, Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from broker_gateway.cp.redaction import filter_headers
from broker_gateway.logging_setup import get_logger
from broker_gateway.metrics import BrokerGatewayMetrics


_log = get_logger("broker_gateway.http")

_SSE_CONTENT_TYPE = "text/event-stream"


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
        log_bodies = _log_inbound_bodies_enabled()

        # Body und Header schon vor call_next ermitteln, damit der
        # Failure-Pfad sie ebenfalls loggen kann. Pre-Read passiert nur,
        # wenn der Request tatsaechlich einen Body hat - sonst wuerde der
        # ASGI-receive-Stream unnoetig konsumiert und SSE-/GET-Endpunkte
        # bekommen Cleanup-Probleme im BaseHTTPMiddleware-Receive-Wrapper.
        request_body_value: Any = None
        request_body_b64: str | None = None
        request_headers_filtered: dict[str, str] | None = None
        if log_bodies:
            request_headers_filtered = filter_headers(request.headers)
            if _request_has_body(request):
                body_bytes = await request.body()
                _replay_request_body(request, body_bytes)
                request_body_value, request_body_b64 = _decode_body(
                    body_bytes, request.headers.get("content-type")
                )

        token = structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            try:
                response = await call_next(request)
            except Exception:
                elapsed = time.monotonic() - start
                self._metrics.requests_total.labels(
                    path=path_template, status="500", scope="-"
                ).inc()
                self._metrics.request_latency_seconds.labels(path=path_template).observe(elapsed)
                err_kwargs: dict[str, Any] = {
                    "request_id": request_id,
                    "method": request.method,
                    "path": path_template,
                    "latency_ms": round(elapsed * 1000.0, 2),
                }
                if log_bodies:
                    err_kwargs["request_headers"] = request_headers_filtered
                    err_kwargs["request_body"] = request_body_value
                    if request_body_b64 is not None:
                        err_kwargs["request_body_b64"] = request_body_b64
                _log.error("http_request_failed", exc_info=True, **err_kwargs)
                raise

            elapsed = time.monotonic() - start
            auth_token = getattr(request.state, "auth_token", None)
            caller_id = auth_token.caller_id if auth_token is not None else None
            scope_list = list(auth_token.scopes) if auth_token is not None else []
            idem_key = request.headers.get("Idempotency-Key")
            status_code = response.status_code

            self._metrics.requests_total.labels(
                path=path_template,
                status=str(status_code),
                scope=",".join(scope_list) or "-",
            ).inc()
            self._metrics.request_latency_seconds.labels(path=path_template).observe(elapsed)

            response_streaming = _is_streaming(response)
            response_body_value: Any = None
            response_body_b64: str | None = None
            if log_bodies and not response_streaming:
                response, body_bytes = await _materialize_response(response)
                response_body_value, response_body_b64 = _decode_body(
                    body_bytes, response.headers.get("content-type")
                )

            log_kwargs: dict[str, Any] = {
                "request_id": request_id,
                "method": request.method,
                "path": path_template,
                "status": status_code,
                "latency_ms": round(elapsed * 1000.0, 2),
                "caller_id": caller_id,
                "scopes": scope_list,
                "idempotency_key": idem_key,
            }
            if log_bodies:
                log_kwargs["request_headers"] = request_headers_filtered
                log_kwargs["request_body"] = request_body_value
                if request_body_b64 is not None:
                    log_kwargs["request_body_b64"] = request_body_b64
                log_kwargs["response_headers"] = filter_headers(response.headers)
                log_kwargs["response_streaming"] = response_streaming
                if response_streaming:
                    log_kwargs["response_body"] = None
                else:
                    log_kwargs["response_body"] = response_body_value
                    if response_body_b64 is not None:
                        log_kwargs["response_body_b64"] = response_body_b64
            _log.info("http_request", **log_kwargs)

            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            structlog.contextvars.reset_contextvars(**token)


def _log_inbound_bodies_enabled() -> bool:
    raw = (os.environ.get("BG_LOG_INBOUND_BODIES") or "on").strip().lower()
    return raw not in ("off", "0", "false", "no")


def _request_has_body(request: Request) -> bool:
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > 0:
        return True
    te = (request.headers.get("transfer-encoding") or "").lower()
    return "chunked" in te


def _replay_request_body(request: Request, body: bytes) -> None:
    """Stellt sicher, dass nachgelagerte Endpunkte den Body weiter sehen.

    ``request.body()`` konsumiert den ASGI-receive-Stream einmalig.
    Wir ersetzen ``request._receive`` durch ein Replay, das den schon
    gelesenen Body als einen einzigen Chunk zurueckspielt - kompatibel
    mit ``await request.body()``, ``request.json()`` und ``request.form()``.
    """
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request._receive = receive  # type: ignore[attr-defined]


def _is_streaming(response: Response) -> bool:
    ct = (response.headers.get("content-type") or "").lower()
    return _SSE_CONTENT_TYPE in ct


async def _materialize_response(response: Response) -> tuple[Response, bytes]:
    """Konsumiert ``response.body_iterator`` und liefert einen neuen Response.

    Header werden uebernommen; ``Content-Length`` wird entfernt, damit
    der neue Response ihn auf Basis des materialisierten Bodies neu
    setzt - sonst entsteht ein Mismatch, falls der ursprungliche
    Iterator den Body in mehreren Chunks geliefert hat.
    """
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    body = b"".join(chunks)

    headers = dict(response.headers)
    headers.pop("content-length", None)
    new_response = Response(
        content=body,
        status_code=response.status_code,
        headers=headers,
        media_type=response.media_type,
    )
    return new_response, body


def _decode_body(body: bytes, content_type: str | None) -> tuple[Any, str | None]:
    """Liefert ``(value, b64)``.

    * ``value`` ist parsed JSON (dict/list/scalar), UTF-8-String oder ``None``.
    * ``b64`` ist nur gesetzt, wenn der Body nicht UTF-8-dekodierbar ist
      (Binaerdaten - dann ist ``value=None`` und der Base64-Fallback dient
      als verlustfreier Forensik-Anker).
    """
    if not body:
        return None, None
    ct_lower = (content_type or "").lower()
    looks_like_json = "json" in ct_lower or body[:1] in (b"{", b"[")
    if looks_like_json:
        try:
            return json.loads(body.decode("utf-8")), None
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    try:
        return body.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, base64.b64encode(body).decode("ascii")


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
