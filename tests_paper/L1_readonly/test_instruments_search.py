"""L1 paper_readonly: Instruments-Lookup gegen Paper-Stack (AP-08 K2)."""
from __future__ import annotations

import httpx
import pytest

from tests_paper._dsl.assertions import assert_error_envelope_v1


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


async def test_search_invalid_symbol_returns_empty_or_error(
    paper_http_client,
) -> None:
    response = await paper_http_client.get(
        "/v1/instruments/search", params={"symbol": "XYZINVALID9999"}
    )
    if response.status_code == 200:
        body = response.json()
        assert isinstance(body, list)
        assert body == []
    else:
        assert 400 <= response.status_code < 500
        assert_error_envelope_v1(response)


async def test_search_requires_instruments_read_scope(
    paper_base_url, paper_admin_token
) -> None:
    """Erzeugt einen Token ohne ``instruments:read``-Scope ueber den
    Bootstrap-Admin-Pfad und prueft 403 mit Error-Envelope."""
    async with httpx.AsyncClient(
        base_url=paper_base_url, timeout=10.0
    ) as admin_client:
        token_resp = await admin_client.post(
            "/v1/auth/token",
            headers={"Authorization": f"Bearer {paper_admin_token}"},
            json={
                "caller_id": "ap08-k2-test",
                "scopes": ["quotes:read"],
            },
        )
        token_resp.raise_for_status()
        scoped_token = token_resp.json().get("value") or token_resp.json().get(
            "token"
        )
        try:
            response = await admin_client.get(
                "/v1/instruments/search",
                params={"symbol": "AAPL"},
                headers={"Authorization": f"Bearer {scoped_token}"},
            )
            assert response.status_code == 403
            assert_error_envelope_v1(response, expected_code="missing_scope")
        finally:
            await admin_client.delete(
                f"/v1/auth/token/{scoped_token}",
                headers={"Authorization": f"Bearer {paper_admin_token}"},
            )


async def test_detail_by_conid_includes_exchange_id_and_calendar_url(
    paper_http_client,
) -> None:
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
