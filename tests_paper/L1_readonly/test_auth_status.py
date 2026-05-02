"""L1 paper_readonly: Auth- und Health-Smoke-Tests (AP-08 K1).

Wenn diese drei Tests gruen sind, ist der Paper-Stack korrekt
deployed, die Session lebt und die Account-ID ist aus dem Paper-
Universum (DU-Praefix oder Whitelist).
"""
from __future__ import annotations

import pytest

from tests_paper._dsl.safety import assert_paper_account


pytestmark = pytest.mark.paper_readonly


async def test_internal_health_returns_authenticated(
    paper_http_client,
) -> None:
    response = await paper_http_client.get("/v1/internal/health")
    assert response.status_code == 200
    body = response.json()
    # Live-Schema (verifiziert gegen broker-gateway-paper 1.23.0):
    # auth_status statt session_status, session_age_s statt _seconds.
    assert body.get("auth_status") == "ok"
    assert body.get("cp_reachable") is True
    assert body.get("accounts_initialized") is True
    session_age = body.get("session_age_s")
    if session_age is not None:
        assert float(session_age) >= 0.0


async def test_internal_health_account_id_is_paper(
    paper_account_id,
) -> None:
    """internal/health enthaelt aktuell kein account_id-Feld; die
    DU-Whitelist wird stattdessen gegen die ``BG_PAPER_ACCOUNT_ID``-
    ENV-Fixture geprueft (selbe Quelle, die der DSL-Layer nutzt)."""
    assert_paper_account(paper_account_id)


async def test_public_health_no_auth_needed(paper_base_url) -> None:
    import httpx  # noqa: PLC0415

    async with httpx.AsyncClient(base_url=paper_base_url, timeout=10.0) as c:
        response = await c.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body.get("status") == "ok"
    assert "version" in body
