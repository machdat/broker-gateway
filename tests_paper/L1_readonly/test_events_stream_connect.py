"""L1 paper_readonly: SSE-Connect-Probe (AP-08 K6).

Test verifiziert, dass der ``/v1/events/stream``-Endpoint mit
gueltigem Token einen ``text/event-stream``-Response anbietet. Der
Stream wird sofort wieder geschlossen - kein Event-Konsum.
"""
from __future__ import annotations

import asyncio

import pytest


pytestmark = pytest.mark.paper_readonly


async def test_events_stream_connect_returns_text_event_stream(
    paper_http_client,
) -> None:
    async with paper_http_client.stream("GET", "/v1/events/stream") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")


async def test_events_stream_requires_auth(paper_base_url) -> None:
    import httpx  # noqa: PLC0415

    async with httpx.AsyncClient(base_url=paper_base_url, timeout=5.0) as c:
        async with c.stream("GET", "/v1/events/stream") as resp:
            assert resp.status_code == 401
