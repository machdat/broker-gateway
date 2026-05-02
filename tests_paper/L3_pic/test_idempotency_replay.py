"""L3 paper_pic: Idempotency-Replay gegen Paper (AP-12 L3-1).

Schaerfster Idempotency-Beweis: Zwei POST /v1/orders mit demselben
Idempotency-Key liefern bitidentisch denselben Body inkl. order_id.
Gleiches fuer DELETE /v1/orders/{id}.

Cleanup wie in test_place_and_cancel.py: jeder Test cancelt alle
offenen Orders im ``finally``.
"""
from __future__ import annotations

import secrets
from decimal import Decimal

import httpx
import pytest

from tests_paper._dsl.actions import (
    cancel_all_open_orders,
    cancel_order,
)
from tests_paper._dsl.assertions import (
    assert_idempotency_replay_returns_original,
)
from tests_paper._dsl.safety import (
    assert_paper_account,
    max_notional_per_order,
)
from tests_paper._dsl.symbols import CONID_AAPL


pytestmark = pytest.mark.paper_pic


async def _place_buy_far_from_market(
    client: httpx.AsyncClient,
    account_id: str,
    *,
    idempotency_key: str,
) -> httpx.Response:
    """Identisch zu place_limit_far_from_market, aber liefert die
    rohe httpx.Response zurueck statt nur die order_id - der
    Idempotency-Test muss auf den Body und Status zugreifen.
    """
    snapshot = await client.get(
        "/v1/quotes/snapshot",
        params={"conids": str(CONID_AAPL), "fields": "last"},
    )
    snapshot.raise_for_status()
    body = snapshot.json()
    if isinstance(body, list) and body:
        last = Decimal(str(body[0].get("last")))
    elif isinstance(body, dict):
        last = Decimal(str(body.get("last")))
    else:
        raise AssertionError(
            f"snapshot fuer conid={CONID_AAPL} ohne 'last': {body!r}"
        )
    factor = Decimal("80") / Decimal("100")  # 20% unter Markt
    limit_price = (last * factor).quantize(Decimal("0.01"))
    qty = Decimal("1")
    max_notional_per_order(limit_price, qty)
    return await client.post(
        "/v1/orders",
        json={
            "account_id": account_id,
            "conid": CONID_AAPL,
            "side": "BUY",
            "quantity": str(qty),
            "order_type": "LMT",
            "limit_price": str(limit_price),
            "tif": "DAY",
        },
        headers={"Idempotency-Key": idempotency_key},
    )


async def test_place_replay_returns_same_body(
    paper_http_client: httpx.AsyncClient,
    paper_account_id: str,
) -> None:
    """Zweiter POST /v1/orders mit gleichem Idempotency-Key = identischer
    Body, Status flippt von 201 auf 200."""
    assert_paper_account(paper_account_id)
    place_key = f"l3-place-replay-{secrets.token_hex(8)}"
    try:
        first = await _place_buy_far_from_market(
            paper_http_client, paper_account_id, idempotency_key=place_key
        )
        assert first.status_code == 201, (
            f"erster POST: erwartet 201, bekam {first.status_code} "
            f"(body={first.text!r})"
        )
        second = await _place_buy_far_from_market(
            paper_http_client, paper_account_id, idempotency_key=place_key
        )
        assert second.status_code == 200, (
            f"zweiter POST: erwartet 200 (Replay), bekam {second.status_code}"
        )
        assert_idempotency_replay_returns_original(
            first.json(), second.json(), must_match_keys=("order_id",)
        )
    finally:
        await cancel_all_open_orders(paper_http_client, paper_account_id)


async def test_cancel_replay_returns_same_body(
    paper_http_client: httpx.AsyncClient,
    paper_account_id: str,
) -> None:
    """Zweiter DELETE /v1/orders/{id} mit gleichem Idempotency-Key liefert
    denselben Body."""
    assert_paper_account(paper_account_id)
    place_key = f"l3-cancel-setup-{secrets.token_hex(8)}"
    cancel_key = f"l3-cancel-replay-{secrets.token_hex(8)}"
    order_id: int | str | None = None
    try:
        place_response = await _place_buy_far_from_market(
            paper_http_client, paper_account_id, idempotency_key=place_key
        )
        assert place_response.status_code == 201
        place_body = place_response.json()
        order_id = place_body.get("order_id") or place_body.get("id")
        assert order_id is not None, f"keine order_id im place-Body: {place_body!r}"

        first_cancel = await cancel_order(
            paper_http_client,
            paper_account_id,
            order_id,
            idempotency_key=cancel_key,
        )
        assert first_cancel.status_code == 200, (
            f"erster DELETE: erwartet 200, bekam {first_cancel.status_code}"
        )
        second_cancel = await cancel_order(
            paper_http_client,
            paper_account_id,
            order_id,
            idempotency_key=cancel_key,
        )
        assert second_cancel.status_code == 200, (
            f"zweiter DELETE: erwartet 200 (Replay), bekam "
            f"{second_cancel.status_code}"
        )
        assert_idempotency_replay_returns_original(
            first_cancel.json(),
            second_cancel.json(),
            must_match_keys=(),
        )
    finally:
        await cancel_all_open_orders(paper_http_client, paper_account_id)
