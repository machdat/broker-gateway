"""L1 paper_readonly: SSE-Connect-Probe (AP-08 K6).

Dieser Test prueft den Handshake-Pfad von ``GET /v1/events/stream``;
echte Event-Frames kommen erst in L4-destructive zustande. Wichtig
ist: nach dem Test muss die Server-seitige Subscription wieder
geschlossen sein - andernfalls leakt der Refcount.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from tests_paper._dsl.assertions import assert_error_envelope_v1


pytestmark = pytest.mark.paper_readonly


async def test_events_stream_connect_and_disconnect(
    paper_http_client,
) -> None:
    async with paper_http_client.stream("GET", "/v1/events/stream") as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
        # Nicht aktiv warten - das Test-Goal ist Handshake, nicht
        # Event-Konsum.
        await asyncio.sleep(0.1)


async def test_events_stream_requires_events_read_scope(
    paper_base_url, paper_admin_token
) -> None:
    async with httpx.AsyncClient(
        base_url=paper_base_url, timeout=10.0
    ) as admin:
        token_resp = await admin.post(
            "/v1/auth/token",
            headers={"Authorization": f"Bearer {paper_admin_token}"},
            json={
                "caller_id": "ap08-k6-test",
                "scopes": ["instruments:read"],
            },
        )
        token_resp.raise_for_status()
        scoped = token_resp.json().get("value") or token_resp.json().get(
            "token"
        )
        try:
            async with admin.stream(
                "GET",
                "/v1/events/stream",
                headers={"Authorization": f"Bearer {scoped}"},
            ) as resp:
                assert resp.status_code == 403
                # Body lesen, damit assert_error_envelope_v1 ihn parsen kann.
                await resp.aread()
                assert_error_envelope_v1(resp, expected_code="missing_scope")
        finally:
            await admin.delete(
                f"/v1/auth/token/{scoped}",
                headers={"Authorization": f"Bearer {paper_admin_token}"},
            )


async def test_events_stream_last_event_id_handshake_accepts_header(
    paper_http_client,
) -> None:
    """Reconnect-Pfad: Server muss den Last-Event-ID-Header akzeptieren
    (kein 4xx). Echtes Replay-Verhalten ist Aufgabe einer L4-Karte."""
    async with paper_http_client.stream(
        "GET",
        "/v1/events/stream",
        headers={"Last-Event-ID": "0"},
    ) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
