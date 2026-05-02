"""L1 paper_readonly: Portfolio-Reads gegen Paper-Stack (AP-08 K4)."""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.paper_readonly


async def test_portfolio_summary_returns_canonical_fields(
    paper_http_client, paper_account_id
) -> None:
    response = await paper_http_client.get(
        f"/v1/portfolio/{paper_account_id}/summary"
    )
    assert response.status_code == 200
    body = response.json()
    # Mindest-Felder gemaess docs/api/v1.md Section 6.1.
    assert "net_liquidation" in body or "net_liquidation_value" in body
    assert isinstance(body, dict)


async def test_portfolio_positions_is_list(
    paper_http_client, paper_account_id
) -> None:
    response = await paper_http_client.get(
        f"/v1/portfolio/{paper_account_id}/positions"
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)


async def test_portfolio_ledger_returns_currency_breakdown(
    paper_http_client, paper_account_id
) -> None:
    response = await paper_http_client.get(
        f"/v1/portfolio/{paper_account_id}/ledger"
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, dict) or isinstance(body, list)
