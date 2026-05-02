"""L1 paper_readonly: Quotes-Snapshot gegen Paper-Stack (AP-08 K3)."""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.paper_readonly


async def _conid_for(symbol: str, client) -> int:
    response = await client.get(
        "/v1/instruments/search", params={"symbol": symbol}
    )
    response.raise_for_status()
    return response.json()[0]["conid"]


async def test_snapshot_three_symbols_returns_all(
    paper_http_client,
) -> None:
    conids = [
        await _conid_for(symbol, paper_http_client)
        for symbol in ("AAPL", "MSFT", "AMZN")
    ]
    response = await paper_http_client.get(
        "/v1/quotes/snapshot",
        params={
            "conids": ",".join(str(c) for c in conids),
            "fields": "last,bid,ask,availability",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    received_conids = {entry.get("conid") for entry in body}
    assert set(conids) == received_conids


async def test_snapshot_availability_field_is_normalised(
    paper_http_client,
) -> None:
    conid = await _conid_for("AAPL", paper_http_client)
    response = await paper_http_client.get(
        "/v1/quotes/snapshot",
        params={"conids": str(conid), "fields": "availability"},
    )
    assert response.status_code == 200
    entry = response.json()[0]
    availability = entry.get("availability")
    if availability is not None:
        assert availability in {"realtime", "delayed", "frozen"}
