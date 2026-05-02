"""L1 paper_readonly: Instruments-Lookup gegen Paper-Stack (AP-08 K2)."""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.paper_readonly


async def test_search_aapl_returns_at_least_one_match(
    paper_http_client,
) -> None:
    response = await paper_http_client.get(
        "/v1/instruments/search", params={"symbol": "AAPL"}
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    assert body, "search AAPL liefert leere Liste"
    first = body[0]
    assert first.get("symbol", "").upper() == "AAPL"
    assert isinstance(first.get("conid"), int)


async def test_info_for_aapl_includes_listing_exchange(
    paper_http_client,
) -> None:
    # Erst searchen, dann conid einsetzen.
    search = await paper_http_client.get(
        "/v1/instruments/search", params={"symbol": "AAPL"}
    )
    search.raise_for_status()
    conid = search.json()[0]["conid"]

    info = await paper_http_client.get(f"/v1/instruments/{conid}")
    assert info.status_code == 200
    body = info.json()
    assert body.get("conid") == conid
    # exchange_id und calendar_url sind seit AP-11 K4 Pflichtfelder.
    assert body.get("exchange_id") is not None
    assert body.get("calendar_url", "").startswith("/v1/exchanges/")


async def test_info_unknown_conid_yields_404(paper_http_client) -> None:
    response = await paper_http_client.get("/v1/instruments/999999999")
    assert response.status_code == 404
