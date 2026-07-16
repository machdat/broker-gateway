"""L3 paper_pic: client_order_id als IBKR-orderRef (Karte 0cfea205).

Verifiziert den Korrelationsschlüssel gegen echtes IBKR-Paper - nicht
gegen Test-Doubles. Der Grund steht im Constraint der Karte: genau
diese Lücke ist im Konsumenten-Repo dreimal an grünen Unit-Tests
vorbeigelaufen, weil das Double konnte, was der echte Adapter nicht
kann. Die Unit-Tests in ``tests/test_tws/test_orders.py``
(``TestClientOrderIdRoundTrip``) decken den Pfad bis ans
ib_async-Objekt ab; erst hier ist IBKR selbst im Spiel.

Cleanup-Disziplin wie im Nachbarmodul ``test_place_and_cancel.py``:
jeder Test ruft ``cancel_all_open_orders`` im ``finally``. Der
Cleanup pollt in mehreren Runden, weil eine gerade platzierte Order im
Moment des ersten GET noch nicht in der offenen Liste sein muss.
Vor US-Marktöffnung verarbeitet IBKR-Paper Cancels verzögert (die Order
bleibt kurz ``Submitted``, obwohl der Cancel mit 200 quittiert wurde) -
der Cleanup ist dort best-effort; die LMT-Orders liegen 20% unter Markt
und können ohnehin nicht fillen.

Aufruf:

    BG_PAPER_BASE_URL=http://cma-pi-1:4001 \\
    BG_PAPER_BOOTSTRAP_TOKEN=<admin-token> \\
    BG_PAPER_ACCOUNT_ID=DUQ312230 \\
    pytest -m paper_pic tests_paper/L3_pic/test_client_order_id.py
"""
from __future__ import annotations

import secrets

import httpx
import pytest

from tests_paper._dsl.actions import (
    cancel_all_open_orders,
    cancel_order,
    modify_order,
    place_limit_far_from_market,
    wait_for_order_status,
)
from tests_paper._dsl.safety import assert_paper_account
from tests_paper._dsl.symbols import CONID_AAPL


pytestmark = pytest.mark.paper_pic


_TIMEOUT_S = 15.0


async def _find_by_client_order_id(
    client: httpx.AsyncClient, account_id: str, client_order_id: str
) -> dict | None:
    """Sucht eine Order in ``GET /v1/orders`` über ``client_order_id``.

    Bewusst NICHT über ``order_id``: genau die wechselt (orderId ->
    permId), sobald IBKR die permId vergibt - der Grund für diese Karte.
    Die aus dem POST zurückgegebene ``order_id`` taucht in der Liste
    deshalb nicht mehr auf. Der stabile Korrelationsschlüssel ist der
    einzige verlässliche Weg, die eigene Order wiederzufinden - und dass
    das funktioniert, ist selbst schon der Nachweis.
    """
    response = await client.get("/v1/orders", params={"account_id": account_id})
    if response.status_code != 200:
        return None
    body = response.json()
    orders = body.get("orders", []) if isinstance(body, dict) else body
    for order in orders:
        if order.get("client_order_id") == client_order_id:
            return order
    return None


async def _get_order(
    client: httpx.AsyncClient, account_id: str, order_id: int | str
) -> dict | None:
    """Liest eine einzelne Order über ``GET /v1/orders/{order_id}``.

    Für Fälle ohne ``client_order_id`` (Regression). Der Lookup-Pfad
    akzeptiert sowohl die orderId als auch die permId, ist gegen den
    ID-Wechsel also robust.
    """
    response = await client.get(f"/v1/orders/{order_id}")
    if response.status_code != 200:
        return None
    return response.json()


async def test_client_order_id_kommt_vom_broker_zurueck(
    paper_http_client: httpx.AsyncClient,
    paper_account_id: str,
) -> None:
    """POST mit client_order_id -> Order trägt ihn, nicht mehr null.

    Kernnachweis der Karte. Vorher war client_order_id auf dem
    TWS-Backend strukturell immer null, weil _build_ib_order orderRef
    nie gesetzt hat.
    """
    assert_paper_account(paper_account_id)
    coid = f"bg-test-{secrets.token_hex(6)}"
    order_id: int | str | None = None
    try:
        order_id = await place_limit_far_from_market(
            paper_http_client,
            paper_account_id,
            CONID_AAPL,
            "BUY",
            distance_pct=20,
            qty=1,
            idempotency_key=f"coid-place-{secrets.token_hex(8)}",
            client_order_id=coid,
        )
        assert order_id is not None

        await wait_for_order_status(
            paper_http_client,
            paper_account_id,
            order_id,
            target=("submitted", "presubmitted", "presubmit"),
            timeout_s=_TIMEOUT_S,
        )
        order = await _find_by_client_order_id(
            paper_http_client, paper_account_id, coid
        )
        assert order is not None, (
            f"Order mit client_order_id={coid!r} nicht in GET /v1/orders - "
            "der Schlüssel kam nicht vom Broker zurück (oder blieb null)"
        )
        assert order["client_order_id"] == coid
    finally:
        await cancel_all_open_orders(paper_http_client, paper_account_id)


async def test_client_order_id_ueberlebt_modify_und_cancel(
    paper_http_client: httpx.AsyncClient,
    paper_account_id: str,
) -> None:
    """Der Schlüssel bleibt über den Lifecycle stabil - die order_id nicht.

    Deckt zwei Verification-Punkte der Karte in einem Durchlauf:

    1. Überlebt orderRef den Modify? IBKR setzt einen Modify als
       cancel/replace um. In ib_async überlebt das Feld mechanisch
       (modify_order mutiert dasselbe Order-Objekt weiter), aber das
       cancel/replace passiert broker-seitig - deshalb hier gemessen.
    2. Der Schlüssel gilt auch im Cancel-Zustand noch.
    """
    assert_paper_account(paper_account_id)
    coid = f"bg-life-{secrets.token_hex(6)}"
    order_id: int | str | None = None
    try:
        order_id = await place_limit_far_from_market(
            paper_http_client,
            paper_account_id,
            CONID_AAPL,
            "BUY",
            distance_pct=20,
            qty=1,
            idempotency_key=f"coid-life-{secrets.token_hex(8)}",
            client_order_id=coid,
        )
        assert order_id is not None
        await wait_for_order_status(
            paper_http_client,
            paper_account_id,
            order_id,
            target=("submitted", "presubmitted", "presubmit"),
            timeout_s=_TIMEOUT_S,
        )
        vorher = await _find_by_client_order_id(
            paper_http_client, paper_account_id, coid
        )
        assert vorher is not None
        assert vorher["client_order_id"] == coid

        # Limit weiter nach unten - bleibt außerhalb des Marktes.
        neues_limit = round(float(vorher["limit_price"]) * 0.95, 2)
        response = await modify_order(
            paper_http_client,
            paper_account_id,
            order_id,
            limit_price=neues_limit,
            idempotency_key=f"coid-mod-{secrets.token_hex(8)}",
        )
        assert response.status_code == 200, (
            f"modify: {response.status_code} (body={response.text!r})"
        )

        # Bewusst NICHT den Modify-Response prüfen: der trägt den orderRef
        # aus demselben lokalen Order-Objekt, das der Adapter mutiert - das
        # wäre nur das eigene Echo und bewiese über den Broker nichts (im
        # Review angemerkt). Stattdessen frisch aus GET /v1/orders lesen,
        # und zwar über den client_order_id selbst: das ist zugleich der
        # Beweis, dass er den cancel/replace des Modify überlebt hat.
        nachher = await _find_by_client_order_id(
            paper_http_client, paper_account_id, coid
        )
        assert nachher is not None, (
            "Order nach Modify nicht mehr über client_order_id auffindbar - "
            f"orderRef hat den cancel/replace NICHT überlebt (coid={coid!r})"
        )
        assert nachher["client_order_id"] == coid

        cancel_response = await cancel_order(
            paper_http_client,
            paper_account_id,
            order_id,
            idempotency_key=f"coid-cancel-{secrets.token_hex(8)}",
        )
        assert cancel_response.status_code in (200, 202)
    finally:
        await cancel_all_open_orders(paper_http_client, paper_account_id)


async def test_ohne_client_order_id_bleibt_das_feld_null(
    paper_http_client: httpx.AsyncClient,
    paper_account_id: str,
) -> None:
    """Regression: bestehende Aufrufer verhalten sich unverändert."""
    assert_paper_account(paper_account_id)
    order_id: int | str | None = None
    try:
        order_id = await place_limit_far_from_market(
            paper_http_client,
            paper_account_id,
            CONID_AAPL,
            "BUY",
            distance_pct=20,
            qty=1,
            idempotency_key=f"coid-none-{secrets.token_hex(8)}",
        )
        assert order_id is not None
        await wait_for_order_status(
            paper_http_client,
            paper_account_id,
            order_id,
            target=("submitted", "presubmitted", "presubmit"),
            timeout_s=_TIMEOUT_S,
        )
        order = await _get_order(paper_http_client, paper_account_id, order_id)
        assert order is not None
        assert order["client_order_id"] is None
    finally:
        await cancel_all_open_orders(paper_http_client, paper_account_id)


async def test_non_ascii_wird_mit_422_abgelehnt(
    paper_http_client: httpx.AsyncClient,
    paper_account_id: str,
) -> None:
    """Der Guard muss VOR IBKR greifen - sonst geht die Order still verloren.

    Gegen Paper gemessen (2026-07-16): ein orderRef mit Umlauten zerlegt
    den TWS-Wire-Stream, IBKR antwortet mit "Error 320: Attempted read
    beyond end of socket stream" und platziert die Order nicht (permId
    bleibt 0). Der Aufrufer bekäme ohne den Guard ein 201 auf eine
    Order, die nie existiert hat.

    Dieser Test platziert bewusst NICHTS: er prüft, dass die Anfrage an
    der API-Grenze mit 422 endet und IBKR nie erreicht.
    """
    assert_paper_account(paper_account_id)
    response = await paper_http_client.post(
        "/v1/orders",
        json={
            "account_id": paper_account_id,
            "conid": CONID_AAPL,
            "side": "BUY",
            "quantity": "1",
            "order_type": "LMT",
            "limit_price": "1.00",
            "tif": "DAY",
            "client_order_id": "bg-umlaut-äöüß",
        },
        headers={"Idempotency-Key": f"coid-bad-{secrets.token_hex(8)}"},
    )
    assert response.status_code == 422, (
        f"Non-ASCII client_order_id: erwartet 422, bekam "
        f"{response.status_code} (body={response.text!r})"
    )
