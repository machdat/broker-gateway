"""L2 paper_safe_write: GET /v1/status gegen den Paper-Stack
(AP-12 L2-2).

Der Status-Endpoint (AP-11 K8) liefert Connectivity- und Push-Health-
Felder. Hier verifiziert gegen die deployed Instanz, dass das Schema
stabil ist und die Auth-Schranke greift.
"""
from __future__ import annotations

import httpx
import pytest

from tests_paper._dsl.assertions import assert_error_envelope_v1


pytestmark = pytest.mark.paper_safe_write


_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "cp_gateway_connected",
        "last_frame_age_seconds",
        "reconnect_attempt",
        "subscriptions_active",
    }
)


async def test_status_returns_v1_envelope(
    paper_http_client: httpx.AsyncClient,
) -> None:
    """GET /v1/status liefert die vier Pflichtfelder mit korrekten Typen."""
    response = await paper_http_client.get("/v1/status")
    assert response.status_code == 200, (
        f"GET /v1/status: erwartet 200, bekam {response.status_code}"
    )
    body = response.json()
    assert isinstance(body, dict), f"erwartet dict, bekam {type(body).__name__}"
    missing = _REQUIRED_FIELDS - set(body)
    assert not missing, f"Pflichtfelder fehlen: {sorted(missing)}"
    assert isinstance(body["cp_gateway_connected"], bool)
    assert isinstance(body["reconnect_attempt"], int)
    assert isinstance(body["subscriptions_active"], int)
    if body["last_frame_age_seconds"] is not None:
        assert isinstance(body["last_frame_age_seconds"], (int, float))


async def test_status_requires_authorization(
    paper_base_url: str,
) -> None:
    """Ohne Bearer liefert /v1/status 401 mit Error-Envelope."""
    async with httpx.AsyncClient(base_url=paper_base_url, timeout=10.0) as anon:
        response = await anon.get("/v1/status")
    assert response.status_code == 401, (
        f"GET /v1/status anon: erwartet 401, bekam {response.status_code}"
    )
    assert_error_envelope_v1(response)
