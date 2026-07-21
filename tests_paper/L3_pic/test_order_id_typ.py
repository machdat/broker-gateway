"""L3 paper_pic: ``order_id`` trägt auf allen Flächen denselben JSON-Typ.

Karte `c1c159d1`. Der Stream-Frame lieferte `order_id` als JSON-Zahl, die
REST-Antwort als String - derselbe Wert in zwei Typen. Ein Konsument, der
über beide Flächen korreliert, verglich `int` gegen `str` und fand nie eine
Übereinstimmung, ohne dass irgendwo ein Fehler auftauchte.

Warum gegen echtes Paper und nicht gegen Doubles: genau diese Frame-Semantik
ist in der Vorgänger-Karte `736c49a5` drei Review-Runden lang an Doubles
vorbeigelaufen. Der Unit-Test `tests/test_orders_stream.py` deckt die
Serialisierung ab; erst hier läuft ein echter IBKR-Frame durch den Stream.

Der geprüfte Vertrag ist der **Typ**, nicht der Wert. Dass POST die
`orderId` und Listen/Stream die `permId` liefern, bleibt so - der stabile
Korrelationsschlüssel ist `client_order_id` (siehe `test_client_order_id.py`).

Aufruf:

    BG_PAPER_BASE_URL=http://cma-pi-1:4001 \\
    BG_PAPER_BOOTSTRAP_TOKEN=<admin-token> \\
    BG_PAPER_ACCOUNT_ID=DUQ312230 \\
    pytest -m paper_pic tests_paper/L3_pic/test_order_id_typ.py
"""
from __future__ import annotations

import asyncio
import json
import secrets

import httpx
import pytest

from tests_paper._dsl.actions import (
    cancel_all_open_orders,
    place_limit_far_from_market,
    subscribe_orders_stream,
    wait_for_order_status,
)
from tests_paper._dsl.safety import assert_paper_account
from tests_paper._dsl.symbols import CONID_AAPL


pytestmark = pytest.mark.paper_pic


_TIMEOUT_S = 15.0
_STREAM_TIMEOUT_S = 20.0


async def _erster_bootstrap_frame(stream: httpx.Response) -> dict:
    """Liest den ersten ``bootstrap``-Block aus dem SSE-Stream.

    Der Stream schickt beim Verbinden den offenen Bestand als
    ``event: bootstrap`` mit ``{"orders": [...]}``. Heartbeat-Kommentare
    (``: keepalive``) werden übersprungen.
    """

    async def _read() -> dict:
        async for line in stream.aiter_lines():
            if not line.startswith("data:"):
                continue
            nutzlast = json.loads(line[len("data:") :].strip())
            if isinstance(nutzlast, dict) and "orders" in nutzlast:
                return nutzlast
        raise AssertionError("Stream beendet ohne bootstrap-Frame")

    return await asyncio.wait_for(_read(), timeout=_STREAM_TIMEOUT_S)


async def test_order_id_ist_ueberall_ein_string(
    paper_http_client: httpx.AsyncClient,
    paper_account_id: str,
) -> None:
    """POST, GET und der SSE-Stream liefern ``order_id`` als String.

    Der Kernnachweis der Karte. Vor dem Fix war der Stream-Wert eine
    JSON-Zahl, während beide REST-Flächen einen String lieferten.
    """
    assert_paper_account(paper_account_id)
    coid = f"bg-idtyp-{secrets.token_hex(6)}"
    try:
        order_id = await place_limit_far_from_market(
            paper_http_client,
            paper_account_id,
            CONID_AAPL,
            "BUY",
            distance_pct=20,
            qty=1,
            idempotency_key=f"idtyp-place-{secrets.token_hex(8)}",
            client_order_id=coid,
        )
        # Der DSL-Helfer reicht den Wert aus dem 201-Body unveraendert durch.
        assert isinstance(order_id, str), (
            f"POST /v1/orders: order_id ist {type(order_id).__name__}, "
            f"erwartet str (Wert={order_id!r})"
        )

        await wait_for_order_status(
            paper_http_client,
            paper_account_id,
            order_id,
            target=("submitted", "presubmitted", "presubmit"),
            timeout_s=_TIMEOUT_S,
        )

        listing = await paper_http_client.get(
            "/v1/orders", params={"account_id": paper_account_id}
        )
        assert listing.status_code == 200, listing.text
        body = listing.json()
        orders = body.get("orders", []) if isinstance(body, dict) else body
        unsere = [o for o in orders if o.get("client_order_id") == coid]
        assert unsere, (
            f"Order mit client_order_id={coid!r} nicht in GET /v1/orders - "
            "ohne sie ist der Typvergleich nicht aussagekräftig"
        )
        for order in orders:
            assert isinstance(order["order_id"], str), (
                f"GET /v1/orders: order_id ist "
                f"{type(order['order_id']).__name__}, erwartet str"
            )

        async with subscribe_orders_stream(
            paper_http_client, paper_account_id
        ) as stream:
            bootstrap = await _erster_bootstrap_frame(stream)
        frames = bootstrap["orders"]
        assert frames, "bootstrap-Frame ohne Orders - Stream nicht aussagekräftig"
        for frame in frames:
            assert isinstance(frame["order_id"], str), (
                f"/v1/orders/stream: order_id ist "
                f"{type(frame['order_id']).__name__}, erwartet str "
                f"(Wert={frame['order_id']!r}) - genau der Bruch aus c1c159d1"
            )
    finally:
        await cancel_all_open_orders(paper_http_client, paper_account_id)
