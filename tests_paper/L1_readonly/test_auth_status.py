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
    assert body.get("session_status") == "ok"
    session_age = body.get("session_age_seconds")
    if session_age is not None:
        assert float(session_age) >= 0.0


async def test_internal_health_account_id_is_paper(
    paper_http_client,
) -> None:
    response = await paper_http_client.get("/v1/internal/health")
    assert response.status_code == 200
    body = response.json()
    account = body.get("account_id") or (body.get("accounts") or [None])[0]
    # assert_paper_account wirft PaperSafetyError, wenn nicht DU/Whitelist.
    assert_paper_account(account)


async def test_public_health_no_auth_needed(paper_base_url) -> None:
    import httpx  # noqa: PLC0415

    async with httpx.AsyncClient(base_url=paper_base_url, timeout=10.0) as c:
        response = await c.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body.get("status") == "ok"
    assert "version" in body
