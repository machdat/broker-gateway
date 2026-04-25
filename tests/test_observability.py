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
