"""L1 paper_readonly: Trades-Reads gegen Paper-Stack (AP-08 K5)."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tests_paper._dsl.assertions import assert_error_envelope_v1


pytestmark = pytest.mark.paper_readonly


def _is_decimalable(value: object) -> bool:
    if value is None:
        return False
    try:
        Decimal(str(value))
        return True
    except Exception:  # noqa: BLE001
        return False


async def test_trades_history_returns_list_with_canonical_fields(
    paper_http_client,
) -> None:
    response = await paper_http_client.get("/v1/trades")
    assert response.status_code == 200
    body = response.json()
    items = body if isinstance(body, list) else body.get("trades", [])
    assert isinstance(items, list)
    for entry in items:
        assert "execution_id" in entry
        assert isinstance(entry.get("conid"), int)
        side = entry.get("side", "").upper()
        assert side in {"BUY", "SELL", "B", "S"}
        # Preis ist Decimal-fähig (Money-Wrapper oder String).
        price = entry.get("price")
        if isinstance(price, dict):
            assert "value" in price and "currency" in price
        else:
            assert _is_decimalable(price)


async def test_trades_aggregates_commissions_mtd_returns_money(
    paper_http_client,
) -> None:
    response = await paper_http_client.get(
        "/v1/trades/aggregates", params={"metric": "commissions_mtd"}
    )
    if response.status_code == 404:
        pytest.skip("aggregates-Endpoint nicht implementiert in dieser Version")
    assert response.status_code == 200
    body = response.json()
    total = body.get("commission_total") or body.get("total")
    if isinstance(total, dict):
        assert "value" in total and "currency" in total
    else:
        assert _is_decimalable(total)


async def test_trades_aggregates_unknown_metric_returns_envelope(
    paper_http_client,
) -> None:
    response = await paper_http_client.get(
        "/v1/trades/aggregates", params={"metric": "foo"}
    )
    if response.status_code == 404:
        pytest.skip("aggregates-Endpoint nicht implementiert in dieser Version")
    assert 400 <= response.status_code < 500
    assert_error_envelope_v1(response)


async def test_trades_requires_portfolio_read_scope(
    paper_base_url, paper_admin_token
) -> None:
    async with httpx.AsyncClient(
        base_url=paper_base_url, timeout=10.0
    ) as admin:
        token_resp = await admin.post(
            "/v1/auth/token",
            headers={"Authorization": f"Bearer {paper_admin_token}"},
            json={
                "caller_id": "ap08-k5-test",
                "scopes": ["instruments:read"],
            },
        )
        token_resp.raise_for_status()
        scoped = token_resp.json().get("value") or token_resp.json().get(
            "token"
        )
        try:
            response = await admin.get(
                "/v1/trades",
                headers={"Authorization": f"Bearer {scoped}"},
            )
            assert response.status_code == 403
            assert_error_envelope_v1(response, expected_code="missing_scope")
        finally:
            await admin.delete(
                f"/v1/auth/token/{scoped}",
                headers={"Authorization": f"Bearer {paper_admin_token}"},
            )
