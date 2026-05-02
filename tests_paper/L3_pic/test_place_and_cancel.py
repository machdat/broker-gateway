"""L3 paper_pic: Place + Immediate Cancel gegen Paper (AP-12 L3-1).

Platziert eine Limit-Order weit weg vom Markt und cancelt sie sofort.
Cleanup-Disziplin: jeder Test ruft ``cancel_all_open_orders`` im
``finally``-Block, damit auch bei Test-Fehlern keine offenen Orders
zurueckbleiben.

Aufruf:

    BG_PAPER_BASE_URL=http://cma-pi-1:4001 \\
    BG_PAPER_BOOTSTRAP_TOKEN=<admin-token> \\
    BG_PAPER_ACCOUNT_ID=DUP799747 \\
    pytest -m paper_pic tests_paper/L3_pic/

Voraussetzungen: Paper-Stack auf 4001 deployed (siehe
``docs/runbooks/paper-account-setup.md``); Konto-ID hat DU-Praefix
(siehe ``tests_paper/_dsl/safety.py``).
"""
from __future__ import annotations

import secrets

import httpx
import pytest

from tests_paper._dsl.actions import (
    cancel_all_open_orders,
    cancel_order,
    place_limit_far_from_market,
    wait_for_order_status,
)
from tests_paper._dsl.assertions import assert_error_envelope_v1
from tests_paper._dsl.safety import assert_paper_account
from tests_paper._dsl.symbols import CONID_AAPL


pytestmark = pytest.mark.paper_pic


_TIMEOUT_S = 15.0


async def test_place_limit_far_from_market_then_cancel(
    paper_http_client: httpx.AsyncClient,
    paper_account_id: str,
) -> None:
    """BUY 1 Stueck AAPL 20% unter Markt; Submit-Status prueft; Cancel."""
    assert_paper_account(paper_account_id)
    place_key = f"l3-place-{secrets.token_hex(8)}"
    cancel_key = f"l3-cancel-{secrets.token_hex(8)}"
    order_id: int | str | None = None
    try:
        order_id = await place_limit_far_from_market(
            paper_http_client,
            paper_account_id,
            CONID_AAPL,
            "BUY",
            distance_pct=20,
            qty=1,
            idempotency_key=place_key,
        )
        assert order_id is not None, "place_limit_far_from_market lieferte keine order_id"

        # Erwartet: irgendein nicht-finaler Status (Submitted, PreSubmitted,
        # Filled-Resistant). IBKR-Statuswerte sind nicht garantiert.
        status = await wait_for_order_status(
            paper_http_client,
            paper_account_id,
            order_id,
            target=("submitted", "presubmitted", "presubmit"),
            timeout_s=_TIMEOUT_S,
        )
        assert status, f"order erreichte keinen Submit-Status: {status!r}"

        # Sofortiger Cancel.
        cancel_response = await cancel_order(
            paper_http_client,
            paper_account_id,
            order_id,
            idempotency_key=cancel_key,
        )
        # cancel_order leitet bei Fehler die HTTP-Antwort weiter; wir
        # akzeptieren 200 (Cancel ok) oder 202 (Cancel angenommen).
        assert cancel_response.status_code in (200, 202), (
            f"cancel: erwartet 200/202, bekam {cancel_response.status_code} "
            f"(body={cancel_response.text!r})"
        )
        # Cancel kann asynchron eintrudeln; wir warten bis zum Cancelled-
        # State, akzeptieren PendingCancel als Zwischenstation.
        final_status = await wait_for_order_status(
            paper_http_client,
            paper_account_id,
            order_id,
            target=("cancelled", "canceled", "pendingcancel"),
            timeout_s=_TIMEOUT_S,
        )
        assert final_status, f"order erreichte keinen Cancel-Status: {final_status!r}"
    finally:
        # Defensiv: Cleanup auch wenn der Test mid-flight ausgestiegen ist.
        await cancel_all_open_orders(paper_http_client, paper_account_id)


async def test_cancel_unknown_order_returns_error(
    paper_http_client: httpx.AsyncClient,
    paper_account_id: str,
) -> None:
    """DELETE /v1/orders/<unbekannt> liefert ein Error-Envelope.

    IBKR-Verhalten ist nicht stabil: in der Recording-Empirie liefert
    der CP-Gateway oft einen 5xx-Upstream-Fehler statt 404, weil das
    interne Routing nicht zwischen 'unbekannte order_id' und 'falsche
    Adresse' unterscheidet. Service-Mapping nimmt das in der
    cp_upstream_error-Klasse (502) auf. Test akzeptiert daher 404
    oder 502 mit korrektem Error-Envelope.
    """
    assert_paper_account(paper_account_id)
    response = await paper_http_client.delete(
        "/v1/orders/9999999999",
        headers={
            "Idempotency-Key": f"l3-unknown-{secrets.token_hex(8)}",
            "X-Account-Id": paper_account_id,
        },
    )
    assert response.status_code in (404, 502), (
        f"cancel unknown: erwartet 404 oder 502, bekam {response.status_code} "
        f"(body={response.text!r})"
    )
    assert_error_envelope_v1(response)
