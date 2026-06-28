"""L3 paper_pic: GTC-STP anlegen, Stop nach oben modifizieren, stornieren.

Karte 35ac9a17 (AP-14). Beweist broker-seitig den vollen Schreib-Lifecycle
fuer GTC-STP-Orders ueber die v1-API: Submission, Modify (cancel/replace),
Storno. Cleanup-Disziplin: ``cancel_all_open_orders`` im ``finally``.

Erfordert eine WRITE-faehige Paper-Session (READ_ONLY_API=no). Im
read_only-Modus liefert POST/PATCH 503 read_only_api - dann skippt dieser
Test sinnvollerweise nicht automatisch, sondern schlaegt fehl; der Stack
muss vor dem Lauf write-faehig deployed sein.

Aufruf:

    BG_PAPER_BASE_URL=http://cma-pi-1:4001 \\
    BG_PAPER_BOOTSTRAP_TOKEN=<admin-token> \\
    BG_PAPER_ACCOUNT_ID=DUP799747 \\
    pytest -m paper_pic tests_paper/L3_pic/test_gtc_stp_modify.py
"""
from __future__ import annotations

import asyncio
import secrets
from decimal import Decimal

import httpx
import pytest

from tests_paper._dsl.actions import (
    cancel_all_open_orders,
    cancel_order,
    modify_order,
    place_stop_far_from_market,
    wait_for_order_status,
)
from tests_paper._dsl.safety import assert_paper_account
from tests_paper._dsl.symbols import CONID_AAPL


pytestmark = pytest.mark.paper_pic


_TIMEOUT_S = 15.0


async def test_gtc_stp_place_modify_up_then_cancel(
    paper_http_client: httpx.AsyncClient,
    paper_account_id: str,
) -> None:
    """SELL-STP (GTC) 20% unter Markt; Stop nach oben modifizieren; cancel."""
    assert_paper_account(paper_account_id)
    place_key = f"l3-stp-place-{secrets.token_hex(8)}"
    modify_key = f"l3-stp-modify-{secrets.token_hex(8)}"
    cancel_key = f"l3-stp-cancel-{secrets.token_hex(8)}"
    order_id: int | str | None = None
    try:
        order_id = await place_stop_far_from_market(
            paper_http_client,
            paper_account_id,
            CONID_AAPL,
            "SELL",
            distance_pct=20,
            qty=1,
            tif="GTC",
            idempotency_key=place_key,
        )
        assert order_id is not None, "place_stop_far_from_market lieferte keine order_id"

        status = await wait_for_order_status(
            paper_http_client,
            paper_account_id,
            order_id,
            target=("submitted", "presubmitted", "presubmit"),
            timeout_s=_TIMEOUT_S,
        )
        assert status, f"GTC-STP erreichte keinen Submit-Status: {status!r}"

        # Order-Detail: Typ/TIF/Stop bestaetigen.
        detail = await paper_http_client.get(f"/v1/orders/{order_id}")
        assert detail.status_code == 200
        body = detail.json()
        assert body["order_type"] == "STP", f"order_type={body.get('order_type')!r}"
        assert body["tif"] == "GTC", f"tif={body.get('tif')!r}"
        assert body.get("stop_price") is not None, "stop_price fehlt"
        old_stop = Decimal(str(body["stop_price"]))

        # Die offene GTC-STP erscheint im Listen-Endpunkt (Karte 2, echte Daten).
        listing = await paper_http_client.get("/v1/orders")
        assert listing.status_code == 200
        listed_ids = {str(o.get("order_id")) for o in listing.json()}
        assert str(order_id) in listed_ids, (
            f"Order {order_id} nicht in der Liste {listed_ids}"
        )

        # Stop nach oben: +5% (hoeher, aber weiterhin unter Markt -> kein Trigger).
        new_stop = (old_stop * Decimal("1.05")).quantize(Decimal("0.01"))
        assert new_stop > old_stop
        mod = await modify_order(
            paper_http_client,
            paper_account_id,
            order_id,
            stop_price=new_stop,
            idempotency_key=modify_key,
        )
        assert mod.status_code == 200, f"modify: {mod.status_code} {mod.text!r}"
        # order_id kann sich beim cancel/replace aendern; permId bleibt i.d.R.
        new_order_id = mod.json().get("order_id") or order_id

        # Neuer Stop sichtbar (kann ein paar Ticks brauchen).
        confirmed: str | None = None
        for _ in range(20):
            d2 = await paper_http_client.get(f"/v1/orders/{new_order_id}")
            if d2.status_code == 200:
                sp = d2.json().get("stop_price")
                if sp is not None and Decimal(str(sp)) == new_stop:
                    confirmed = str(sp)
                    break
            await asyncio.sleep(0.3)
        assert confirmed is not None, f"neuer Stop {new_stop} wurde nicht bestaetigt"

        # Storno.
        cancel = await cancel_order(
            paper_http_client,
            paper_account_id,
            new_order_id,
            idempotency_key=cancel_key,
        )
        assert cancel.status_code in (200, 202), (
            f"cancel: erwartet 200/202, bekam {cancel.status_code} ({cancel.text!r})"
        )
        final = await wait_for_order_status(
            paper_http_client,
            paper_account_id,
            new_order_id,
            target=("cancelled", "canceled", "pendingcancel"),
            timeout_s=_TIMEOUT_S,
        )
        assert final, f"GTC-STP erreichte keinen Cancel-Status: {final!r}"
    finally:
        await cancel_all_open_orders(paper_http_client, paper_account_id)
