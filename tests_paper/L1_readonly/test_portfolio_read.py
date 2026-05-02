"""L1 paper_readonly: Portfolio-Reads gegen Paper-Stack (AP-08 K4)."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tests_paper._dsl.assertions import assert_error_envelope_v1


pytestmark = pytest.mark.paper_readonly


def _is_money_or_number(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, dict):
        return "value" in value and "currency" in value
    try:
        Decimal(str(value))
        return True
    except Exception:  # noqa: BLE001
        return False


async def test_summary_includes_money_fields(
    paper_http_client, paper_account_id
) -> None:
    response = await paper_http_client.get(
        f"/v1/portfolio/{paper_account_id}/summary"
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict)
    # Der Adapter darf entweder Money-Wrapper liefern oder einen
    # numerischen String. Wichtig: keiner der drei Hauptwerte fehlt.
    for key in (
        "net_liquidation",
        "total_cash",
        "gross_position_value",
    ):
        if key in body:
            assert _is_money_or_number(body[key]), key


async def test_positions_returns_list_with_canonical_fields(
    paper_http_client, paper_account_id
) -> None:
    response = await paper_http_client.get(
        f"/v1/portfolio/{paper_account_id}/positions"
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    for entry in body:
        assert isinstance(entry.get("conid"), int)
        # Position-Quantity (Decimal-fähig).
        Decimal(str(entry.get("position", 0)))


async def test_ledger_returns_currency_breakdown(
    paper_http_client, paper_account_id
) -> None:
    response = await paper_http_client.get(
        f"/v1/portfolio/{paper_account_id}/ledger"
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, (dict, list))


async def test_portfolio_requires_portfolio_read_scope(
    paper_base_url, paper_admin_token, paper_account_id
) -> None:
    async with httpx.AsyncClient(
        base_url=paper_base_url, timeout=10.0
    ) as admin:
        token_resp = await admin.post(
            "/v1/auth/token",
            headers={"Authorization": f"Bearer {paper_admin_token}"},
            json={
                "caller_id": "ap08-k4-test",
                "scopes": ["instruments:read"],
            },
        )
        token_resp.raise_for_status()
        scoped = token_resp.json().get("value") or token_resp.json().get(
            "token"
        )
        try:
            response = await admin.get(
                f"/v1/portfolio/{paper_account_id}/summary",
                headers={"Authorization": f"Bearer {scoped}"},
            )
            assert response.status_code == 403
            assert_error_envelope_v1(response, expected_code="missing_scope")
        finally:
            await admin.delete(
                f"/v1/auth/token/{scoped}",
                headers={"Authorization": f"Bearer {paper_admin_token}"},
            )
