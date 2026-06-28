"""L1 paper_readonly: GET /v1/orders gegen Paper-Stack (Karte def3e8f5, AP-14).

Contract-Test fuer den Listen-Endpunkt offener Orders. Read-only: prueft
das Antwort-Schema (200 + Array, kanonische Order-Felder inkl. additivem
oca_group). Das Listen mit *echten* offenen GTC-STP-Orders wird zusammen
mit der Schwesterkarte (GTC-STP anlegen/modify) verifiziert, da im
read_only-Paper-Modus keine Order platziert werden kann.
"""
from __future__ import annotations

import httpx
import pytest

from tests_paper._dsl.assertions import assert_error_envelope_v1


pytestmark = pytest.mark.paper_readonly


async def test_list_orders_returns_array(paper_http_client) -> None:
    """GET /v1/orders liefert ein Array offener Orders (ggf. leer)."""
    response = await paper_http_client.get("/v1/orders")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    # Falls offene Orders existieren: kanonische Felder + additive Keys.
    for entry in body:
        assert isinstance(entry.get("conid"), int)
        assert isinstance(entry.get("order_id"), str)
        assert isinstance(entry.get("account_id"), str)
        assert "order_type" in entry
        assert "status" in entry
        # additive Felder (Karte def3e8f5): Key immer vorhanden, Wert
        # darf None sein.
        assert "stop_price" in entry
        assert "oca_group" in entry


async def test_list_orders_account_filter(
    paper_http_client, paper_account_id
) -> None:
    """Der account_id-Filter liefert nur Orders dieses Kontos (oder leer)."""
    response = await paper_http_client.get(
        "/v1/orders", params={"account_id": paper_account_id}
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    for entry in body:
        assert entry.get("account_id") == paper_account_id


async def test_list_orders_requires_orders_read_scope(
    paper_base_url, paper_admin_token
) -> None:
    """Falscher Scope (instruments:read) -> 403 missing_scope."""
    async with httpx.AsyncClient(
        base_url=paper_base_url, timeout=10.0
    ) as admin:
        token_resp = await admin.post(
            "/v1/auth/token",
            headers={"Authorization": f"Bearer {paper_admin_token}"},
            json={"caller_id": "ap14-k2-test", "scopes": ["instruments:read"]},
        )
        token_resp.raise_for_status()
        scoped = token_resp.json().get("value") or token_resp.json().get(
            "token"
        )
        try:
            response = await admin.get(
                "/v1/orders",
                headers={"Authorization": f"Bearer {scoped}"},
            )
            assert response.status_code == 403
            assert_error_envelope_v1(response, expected_code="missing_scope")
        finally:
            await admin.delete(
                f"/v1/auth/token/{scoped}",
                headers={"Authorization": f"Bearer {paper_admin_token}"},
            )
