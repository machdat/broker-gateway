"""L1 paper_readonly: Quotes-Snapshot gegen Paper-Stack (AP-08 K3)."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest

from tests_paper._dsl.assertions import assert_error_envelope_v1
from tests_paper._dsl.symbols import CONID_AAPL, CONID_MSFT, CONID_SAP


pytestmark = pytest.mark.paper_readonly


_VALID_AVAILABILITY = {"realtime", "delayed", "frozen"}


async def test_snapshot_three_symbols(paper_http_client) -> None:
    conids = [CONID_AAPL, CONID_MSFT, CONID_SAP]
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
    received = {entry.get("conid") for entry in body}
    assert set(conids).issubset(received)


async def test_snapshot_availability_is_normalised(
    paper_http_client,
) -> None:
    response = await paper_http_client.get(
        "/v1/quotes/snapshot",
        params={"conids": str(CONID_AAPL), "fields": "availability"},
    )
    assert response.status_code == 200
    entry = response.json()[0]
    availability = entry.get("availability")
    if availability is not None:
        assert availability in _VALID_AVAILABILITY


async def test_first_call_priming_returns_more_fields_on_second_call(
    paper_http_client,
) -> None:
    """broker-gateway-CP-Quirk (Anhang 1.5): erster snapshot liefert
    leere Felder; der Adapter primt intern und liefert beim zweiten
    Call innerhalb weniger Sekunden vollstaendige Werte."""
    params = {
        "conids": str(CONID_AAPL),
        "fields": "last,bid,ask",
    }
    first = await paper_http_client.get(
        "/v1/quotes/snapshot", params=params
    )
    assert first.status_code == 200
    second = await paper_http_client.get(
        "/v1/quotes/snapshot", params=params
    )
    assert second.status_code == 200
    second_body = second.json()[0]
    # Mindestens eines der Preisfelder soll im zweiten Call befuellt sein.
    populated = {
        key: second_body.get(key)
        for key in ("last", "bid", "ask")
        if second_body.get(key) is not None
    }
    assert populated, (
        "second snapshot liefert keine Preisfelder; CP-Adapter primt "
        "moeglicherweise nicht oder Markt ist geschlossen mit availability=frozen"
    )
    for value in populated.values():
        # Werte sind als String formatiert (siehe Quote-Schema).
        Decimal(str(value))  # parsen muss klappen


async def test_snapshot_volume_in_plausible_range(paper_http_client) -> None:
    """Live-Volumen muss plausibel sein (nicht der alte 7762-rohe-Wert).

    Hintergrund: bis v1.24.0 lieferte das volume-Feld den IBKR-7762-
    Wert unverändert (skalierter Integer ~10^14). Ab v1.25.0 dividiert
    der Mapper durch 10^6, sodass eine Aktien-Anzahl im Bereich
    10^3..10^10 herauskommt. Bei geschlossenem Markt darf der Wert
    null sein - dann skippt der Test.
    """
    response = await paper_http_client.get(
        "/v1/quotes/snapshot",
        params={"conids": str(CONID_AAPL), "fields": "volume"},
    )
    assert response.status_code == 200
    entry = response.json()[0]
    raw = entry.get("volume")
    if raw is None:
        pytest.skip("Markt geschlossen oder Subscription noch nicht warm")
    value = int(raw)
    assert 1_000 <= value <= 10_000_000_000, (
        f"AAPL volume={value} ausserhalb plausibler Aktien-Range; "
        f"vermutlich Field-7762-Skalierung gedriftet"
    )


async def test_snapshot_requires_quotes_read_scope(
    paper_base_url, paper_admin_token
) -> None:
    async with httpx.AsyncClient(
        base_url=paper_base_url, timeout=10.0
    ) as admin:
        token_resp = await admin.post(
            "/v1/auth/token",
            headers={"Authorization": f"Bearer {paper_admin_token}"},
            json={
                "caller_id": "ap08-k3-test",
                "scopes": ["instruments:read"],
            },
        )
        token_resp.raise_for_status()
        scoped_token = token_resp.json().get("value") or token_resp.json().get(
            "token"
        )
        try:
            response = await admin.get(
                "/v1/quotes/snapshot",
                params={"conids": str(CONID_AAPL), "fields": "last"},
                headers={"Authorization": f"Bearer {scoped_token}"},
            )
            assert response.status_code == 403
            assert_error_envelope_v1(response, expected_code="missing_scope")
        finally:
            await admin.delete(
                f"/v1/auth/token/{scoped_token}",
                headers={"Authorization": f"Bearer {paper_admin_token}"},
            )
