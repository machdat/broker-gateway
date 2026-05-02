"""L1 paper_readonly: Trades-Reads gegen Paper-Stack (AP-08 K5)."""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.paper_readonly


async def test_trades_history_returns_list(paper_http_client) -> None:
    response = await paper_http_client.get("/v1/trades")
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list) or (
        isinstance(body, dict) and "trades" in body
    )


async def test_trades_aggregates_returns_dict(paper_http_client) -> None:
    # Aggregates-Endpoint: optional, kann 200 oder 404 sein, je nach
    # Service-Version. Wir akzeptieren beides solange das Schema
    # konsistent ist.
    response = await paper_http_client.get("/v1/trades/aggregates")
    assert response.status_code in {200, 404}
    if response.status_code == 200:
        body = response.json()
        assert isinstance(body, dict)
