"""Tests fuer Observability: structured Logs + Prometheus /metrics."""
from __future__ import annotations

import json
import logging

import httpx
import pytest

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_PORTFOLIO_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore, generate_token_value
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle
from broker_gateway.main import create_app
from broker_gateway.metrics import BrokerGatewayMetrics


_ADMIN_VALUE = "obs-admin-token-aaaaaaaaaaaaaaaaa"


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(Token(value=_ADMIN_VALUE, caller_id="bootstrap-admin", scopes=[SCOPE_ADMIN_ALL]))
    return s


@pytest.fixture
async def cp_client(cp_gateway_mock) -> CPGatewayClient:
    client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    yield client
    await client.aclose()


@pytest.fixture
async def lifecycle(cp_client: CPGatewayClient) -> AuthLifecycle:
    lc = AuthLifecycle(cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0)
    yield lc
    await lc.stop()


# ---- Metrics-Endpoint ----


async def test_metrics_endpoint_returns_prometheus_text(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    # Pflicht-Familien:
    assert "broker_gateway_requests_total" in body
    assert "broker_gateway_request_latency_seconds" in body
    assert "broker_gateway_session_age_seconds" in body
    assert "broker_gateway_subscription_count" in body
    assert "broker_gateway_throttle_extra_wait_seconds" in body
    assert "broker_gateway_pacing_violations_total" in body


async def test_requests_counter_increments(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    metrics = BrokerGatewayMetrics()
    application = create_app(store=store, lifecycle=lifecycle, metrics=metrics)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            for _ in range(3):
                await ac.get("/v1/health")

    body = metrics.render().decode("utf-8")
    # Mindestens 3 OK-Requests auf /v1/health (200, scope=- weil unauthentifiziert).
    health_lines = [
        ln for ln in body.splitlines()
        if ln.startswith("broker_gateway_requests_total{")
        and 'path="/v1/health"' in ln
        and 'status="200"' in ln
    ]
    assert health_lines, body
    # Wert ist die letzte Zahl in der Zeile.
    value = float(health_lines[0].rsplit(" ", 1)[-1])
    assert value >= 3.0


async def test_latency_histogram_populated(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    metrics = BrokerGatewayMetrics()
    application = create_app(store=store, lifecycle=lifecycle, metrics=metrics)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            await ac.get("/v1/health")

    body = metrics.render().decode("utf-8")
    assert "broker_gateway_request_latency_seconds_bucket{" in body
    assert 'path="/v1/health"' in body


# ---- Auth-Information im Log + kein Token-Wert ----


async def test_log_contains_caller_id_but_not_token_value(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
    capsys: pytest.CaptureFixture,
) -> None:
    psm_token = generate_token_value()
    store.put(Token(value=psm_token, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))
    application = create_app(store=store, lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            await ac.get(
                "/v1/portfolio/U25235077",
                headers={"Authorization": f"Bearer {psm_token}"},
            )

    captured = capsys.readouterr().out
    # Mindestens eine Zeile sollte http_request enthalten.
    request_lines = [ln for ln in captured.splitlines() if '"event": "http_request"' in ln]
    assert request_lines, captured

    # Pflichtfelder pruefen:
    payload = json.loads(request_lines[-1])
    assert payload["event"] == "http_request"
    assert payload["caller_id"] == "psm"
    assert "portfolio:read" in payload["scopes"]
    assert payload["status"] == 200
    assert "latency_ms" in payload
    assert "request_id" in payload

    # Token-Wert darf NIRGENDS im Log auftauchen.
    assert psm_token not in captured


async def test_log_caller_id_none_when_unauthenticated(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
    capsys: pytest.CaptureFixture,
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            await ac.get("/v1/health")  # /v1/health ist offen

    captured = capsys.readouterr().out
    request_lines = [ln for ln in captured.splitlines() if '"event": "http_request"' in ln]
    assert request_lines
    payload = json.loads(request_lines[-1])
    assert payload["caller_id"] is None
    assert payload["scopes"] == []


async def test_response_has_request_id_header(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    application = create_app(store=store, lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.get("/v1/health")
    assert "x-request-id" in response.headers
    assert len(response.headers["x-request-id"]) >= 16


# ---- Body-Logging (AP-05 Karte 2) ----


def _last_http_request_event(captured: str) -> dict:
    lines = [ln for ln in captured.splitlines() if '"event": "http_request"' in ln]
    assert lines, captured
    return json.loads(lines[-1])


async def test_body_logging_includes_response_body_for_json_path(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
    capsys: pytest.CaptureFixture,
) -> None:
    """JSON-Pfad: response_body erscheint als geparste Struktur, request_body
    bleibt None bei einem GET ohne Body."""
    application = create_app(store=store, lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.get("/v1/health")

    payload = _last_http_request_event(capsys.readouterr().out)
    assert payload["request_headers"] is not None
    assert payload["response_headers"] is not None
    assert payload["request_body"] is None
    assert payload["response_streaming"] is False
    assert payload["response_body"] == {"status": "ok", "version": response.json()["version"]}


async def test_body_logging_includes_request_body_for_post(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
    capsys: pytest.CaptureFixture,
) -> None:
    """POST mit JSON-Body: request_body wird 1:1 als geparste Struktur geloggt."""
    application = create_app(store=store, lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.post(
                "/v1/auth/token",
                headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
                json={"caller_id": "psm-test", "scopes": [SCOPE_PORTFOLIO_READ]},
            )

    assert response.status_code == 201
    payload = _last_http_request_event(capsys.readouterr().out)
    assert payload["request_body"] == {
        "caller_id": "psm-test",
        "scopes": [SCOPE_PORTFOLIO_READ],
    }


async def test_body_logging_validation_error_path(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
    capsys: pytest.CaptureFixture,
) -> None:
    """422-Pfad: request_body bleibt erhalten, response_body enthaelt das Error-Objekt."""
    application = create_app(store=store, lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.post(
                "/v1/auth/token",
                headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
                json={"caller_id": "psm-test", "scopes": ["does:not:exist"]},
            )

    assert response.status_code == 422
    payload = _last_http_request_event(capsys.readouterr().out)
    assert payload["status"] == 422
    assert payload["request_body"] == {
        "caller_id": "psm-test",
        "scopes": ["does:not:exist"],
    }
    assert isinstance(payload["response_body"], dict)
    assert "error" in payload["response_body"]


async def test_body_logging_never_leaks_authorization_header(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
    capsys: pytest.CaptureFixture,
) -> None:
    """Authorization, Cookie, X-API-Key, X-Auth-Token werden gefiltert -
    auch wenn der Request mit Bearer-Token kommt."""
    psm_token = generate_token_value()
    store.put(Token(value=psm_token, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))
    application = create_app(store=store, lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            await ac.get(
                "/v1/portfolio/U25235077",
                headers={
                    "Authorization": f"Bearer {psm_token}",
                    "Cookie": "csrftoken=should-not-appear",
                    "X-API-Key": "leaked-key",
                    "X-Auth-Token": "leaked-auth",
                },
            )

    captured = capsys.readouterr().out
    payload = _last_http_request_event(captured)
    request_header_keys = {k.lower() for k in payload["request_headers"]}
    assert "authorization" not in request_header_keys
    assert "cookie" not in request_header_keys
    assert "x-api-key" not in request_header_keys
    assert "x-auth-token" not in request_header_keys

    # Doppelter Boden: kein Sekret-Wert taucht im gesamten capsys-Output auf.
    assert psm_token not in captured
    assert "csrftoken" not in captured
    assert "leaked-key" not in captured
    assert "leaked-auth" not in captured


async def test_body_logging_disabled_via_env(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BG_LOG_INBOUND_BODIES=off entfernt body- und header-Felder; Metadaten bleiben."""
    monkeypatch.setenv("BG_LOG_INBOUND_BODIES", "off")
    application = create_app(store=store, lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            await ac.get("/v1/health")

    payload = _last_http_request_event(capsys.readouterr().out)
    # Metadaten muessen unveraendert bleiben.
    for key in (
        "request_id", "method", "path", "status",
        "latency_ms", "caller_id", "scopes", "idempotency_key",
    ):
        assert key in payload, f"Metadaten-Feld {key} fehlt bei BODIES=off"
    # Body-/Header-Felder duerfen NICHT im Event sein.
    for key in (
        "request_body", "request_headers",
        "response_body", "response_headers", "response_streaming",
    ):
        assert key not in payload, f"Feld {key} darf bei BODIES=off nicht erscheinen"


async def test_endpoint_still_receives_body_after_middleware_read(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    cp_gateway_mock,
) -> None:
    """Stream-Replacement: nachgelagerter Endpunkt liest den Body weiterhin
    vollstaendig. Beweis: POST /v1/auth/token erstellt erfolgreich (201) -
    waere der Body durch das Middleware-Read verbraucht, wuerde Pydantic 422
    werfen, weil caller_id/scopes Pflicht sind."""
    application = create_app(store=store, lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=application)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        async with application.router.lifespan_context(application):
            response = await ac.post(
                "/v1/auth/token",
                headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
                json={"caller_id": "stream-replace-test", "scopes": [SCOPE_PORTFOLIO_READ]},
            )

    assert response.status_code == 201, (
        f"Endpunkt hat den Body nicht gesehen - Stream-Replacement defekt: {response.text}"
    )
    body = response.json()
    assert body["caller_id"] == "stream-replace-test"


async def test_sse_response_marked_as_streaming(
    capsys: pytest.CaptureFixture,
) -> None:
    """SSE-Endpunkt liefert response_streaming=true und kein response_body.

    Test gegen eine minimale FastAPI-Mock-App, weil der vollstaendige
    /v1/events/stream-Endpunkt asynchrone EventBus-Hooks aufbaut und beim
    schnellen Disconnect race-prone Cleanup-Fehler in BaseHTTPMiddleware
    triggert (nicht relevant fuer das Verhalten der Middleware selbst).
    """
    from fastapi import FastAPI
    from starlette.responses import StreamingResponse

    from broker_gateway.metrics import BrokerGatewayMetrics
    from broker_gateway.middleware.observability import ObservabilityMiddleware

    sse_app = FastAPI()
    sse_app.add_middleware(ObservabilityMiddleware, metrics=BrokerGatewayMetrics())

    @sse_app.get("/sse")
    async def sse() -> StreamingResponse:
        async def gen():
            yield b": ping\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    transport = httpx.ASGITransport(app=sse_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.get("/sse")
        await response.aread()

    payload = _last_http_request_event(capsys.readouterr().out)
    assert payload["response_streaming"] is True
    assert payload["response_body"] is None
    assert "text/event-stream" in payload["response_headers"]["content-type"]
