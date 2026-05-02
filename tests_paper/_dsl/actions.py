"""Aktion-Layer fuer Paper-Tests (AP-07 K5).

Wiederverwendbare DSL-Operationen fuer L2-L4-Tests. Jede schreibende
Funktion ruft vor dem CP-Gateway-Call mindestens
:func:`tests_paper._dsl.safety.assert_paper_account`. Notional-Checks
laufen ueber :func:`max_notional_per_order` bzw.
:func:`assert_within_paper_limits`.

Funktionsuebersicht
-------------------

- ``place_limit_far_from_market`` - platziert eine Limit-Order
  ``distance_pct`` von der zuletzt gesehenen Marktkurve weg
  (Default 20 Prozent, Minimum 5 Prozent - der Constraint verhindert
  versehentliches Fill).
- ``cancel_order`` - DELETE /v1/orders/{id}.
- ``cancel_all_open_orders`` - GET /v1/orders, DELETE pro Order.
- ``flatten_positions`` - schliesst alle Positionen via Market-Order.
- ``subscribe_quote_stream`` - async-Context-Manager ueber SSE.
- ``wait_for_order_status`` - pollt /v1/orders/{id} bis Target.

Plus die ``PaperActions``-Klasse aus K2 als bequemer Wrapper.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any, AsyncIterator, Iterable

import httpx

from tests_paper._dsl.safety import (
    PaperSafetyError,
    assert_paper_account,
    assert_within_paper_limits,
    kill_switch_active,
    max_notional_per_order,
)


_MIN_DISTANCE_PCT = Decimal("5.0")
_DEFAULT_DISTANCE_PCT = Decimal("20.0")


# ---------------------------------------------------------------------------
# Funktionale DSL (Karten-Soll)
# ---------------------------------------------------------------------------


async def place_limit_far_from_market(
    client: httpx.AsyncClient,
    account_id: str,
    conid: int,
    side: str,
    *,
    distance_pct: Decimal | float = _DEFAULT_DISTANCE_PCT,
    qty: Decimal | int = 1,
    idempotency_key: str | None = None,
) -> int | None:
    """Platziert eine Limit-Order weit weg vom Markt.

    Holt zuerst den letzten Quote (``GET /v1/quotes/snapshot``) und
    leitet einen Limit-Preis ab, der ``distance_pct`` Prozent unter
    (BUY) bzw. ueber (SELL) dem letzten Quote liegt. Liefert die
    ``order_id`` aus dem 201-Body zurueck oder ``None``, wenn der
    Server eine andere Form liefert.
    """
    if kill_switch_active():
        raise PaperSafetyError(
            "BG_PAPER_TESTS_DISABLED gesetzt - place_limit_far_from_market "
            "wird abgelehnt."
        )
    side_norm = side.strip().upper()
    if side_norm not in {"BUY", "SELL"}:
        raise PaperSafetyError(f"side {side!r} muss BUY oder SELL sein")
    distance = Decimal(str(distance_pct))
    if distance < _MIN_DISTANCE_PCT:
        raise PaperSafetyError(
            f"distance_pct {distance} unter Minimum {_MIN_DISTANCE_PCT}% - "
            "verhindert versehentlichen Fill."
        )
    assert_paper_account(account_id)

    snapshot = await client.get(
        "/v1/quotes/snapshot", params={"conids": str(conid), "fields": "last"}
    )
    snapshot.raise_for_status()
    snapshot_body = snapshot.json()
    if isinstance(snapshot_body, list) and snapshot_body:
        last_raw = snapshot_body[0].get("last")
    elif isinstance(snapshot_body, dict):
        last_raw = snapshot_body.get("last")
    else:
        last_raw = None
    if not last_raw:
        raise PaperSafetyError(
            f"snapshot fuer conid={conid} liefert kein 'last' - "
            "Limit-Far-Order nicht ableitbar."
        )
    last = Decimal(str(last_raw))
    factor = (Decimal("100") - distance) / Decimal("100")
    if side_norm == "BUY":
        limit_price = (last * factor).quantize(Decimal("0.01"))
    else:
        sell_factor = (Decimal("100") + distance) / Decimal("100")
        limit_price = (last * sell_factor).quantize(Decimal("0.01"))

    qty_d = Decimal(str(qty))
    max_notional_per_order(limit_price, qty_d)

    body: dict[str, Any] = {
        "account": account_id,
        "conid": conid,
        "side": side_norm,
        "quantity": str(qty_d),
        "order_type": "LMT",
        "limit_price": str(limit_price),
        "time_in_force": "DAY",
    }
    headers = (
        {"Idempotency-Key": idempotency_key} if idempotency_key else None
    )
    response = await client.post("/v1/orders", json=body, headers=headers)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        return payload.get("order_id") or payload.get("id")
    return None


async def cancel_order(
    client: httpx.AsyncClient,
    account_id: str,
    order_id: int | str,
    *,
    idempotency_key: str | None = None,
) -> httpx.Response:
    """``DELETE /v1/orders/{order_id}`` mit optionalem Idempotency-Key."""
    if kill_switch_active():
        raise PaperSafetyError(
            "BG_PAPER_TESTS_DISABLED gesetzt - cancel_order abgelehnt."
        )
    assert_paper_account(account_id)
    headers = (
        {"Idempotency-Key": idempotency_key} if idempotency_key else None
    )
    return await client.delete(f"/v1/orders/{order_id}", headers=headers)


async def cancel_all_open_orders(
    client: httpx.AsyncClient,
    account_id: str,
) -> list[Any]:
    """Liest ``GET /v1/orders?account=`` und DELETEt pro Order.

    Idempotent: wenn keine offenen Orders, liefert ``[]``. Liefert die
    Liste der gecancelten ``order_id``-Werte.
    """
    assert_paper_account(account_id)
    response = await client.get(
        "/v1/orders", params={"account": account_id}
    )
    if response.status_code != 200:
        return []
    body = response.json()
    if isinstance(body, dict):
        orders = body.get("orders") or []
    elif isinstance(body, list):
        orders = body
    else:
        orders = []
    cancelled: list[Any] = []
    for order in orders:
        order_id = (
            order.get("order_id")
            or order.get("id")
            or order.get("orderId")
        )
        if order_id is None:
            continue
        await cancel_order(client, account_id, order_id)
        cancelled.append(order_id)
    return cancelled


async def flatten_positions(
    client: httpx.AsyncClient,
    account_id: str,
) -> list[Any]:
    """Schliesst alle Positionen ueber Market-Order in Gegenrichtung.

    Bewusst restriktiv: Pro Position wird ``max_notional_per_order``
    aufgerufen - bei Ueberschreitung bricht die Funktion ab und liefert
    bisher geschlossene IDs zurueck.
    """
    if kill_switch_active():
        raise PaperSafetyError(
            "BG_PAPER_TESTS_DISABLED gesetzt - flatten_positions abgelehnt."
        )
    assert_paper_account(account_id)
    positions_resp = await client.get(
        f"/v1/portfolio/{account_id}/positions"
    )
    if positions_resp.status_code != 200:
        return []
    positions = positions_resp.json()
    if not isinstance(positions, list):
        return []
    closed: list[Any] = []
    for pos in positions:
        position = Decimal(str(pos.get("position", 0)))
        if position == 0:
            continue
        side = "SELL" if position > 0 else "BUY"
        quantity = abs(position)
        last_price = Decimal(str(pos.get("last") or pos.get("avg_price") or 0))
        max_notional_per_order(last_price, quantity)
        body = {
            "account": account_id,
            "conid": pos.get("conid"),
            "side": side,
            "quantity": str(quantity),
            "order_type": "MKT",
            "time_in_force": "DAY",
        }
        response = await client.post("/v1/orders", json=body)
        if response.status_code in (200, 201):
            payload = response.json()
            order_id = (
                payload.get("order_id")
                or payload.get("id")
                if isinstance(payload, dict)
                else None
            )
            if order_id is not None:
                closed.append(order_id)
    return closed


@asynccontextmanager
async def subscribe_quote_stream(
    client: httpx.AsyncClient,
    conids: Iterable[int],
    *,
    fields: list[str],
) -> AsyncIterator[httpx.Response]:
    """Async-Context-Manager um den SSE-Stream.

    Wirft ``RuntimeError`` bei leerer ``fields``-Liste. Beim Verlassen
    des ``async with``-Blocks wird die Stream-Connection geschlossen,
    sodass der Refcount im SubscriptionManager auf 0 faellt.
    """
    if not fields:
        raise RuntimeError("subscribe_quote_stream: fields-Liste leer")
    conid_list = ",".join(str(c) for c in conids)
    if not conid_list:
        raise RuntimeError("subscribe_quote_stream: conids-Liste leer")
    params = {"conids": conid_list, "fields": ",".join(fields)}
    async with client.stream(
        "GET", "/v1/quotes/stream", params=params
    ) as response:
        yield response


async def wait_for_order_status(
    client: httpx.AsyncClient,
    account_id: str,
    order_id: int | str,
    *,
    target: str | Iterable[str],
    timeout_s: float = 10.0,
    poll_interval_s: float = 0.2,
) -> str:
    """Pollt ``/v1/orders/{order_id}`` bis ``target`` erreicht.

    ``target`` darf ein einzelner Status-String oder eine Iterable
    sein (case-insensitive). Liefert den finalen Status zurueck;
    wirft ``TimeoutError``, wenn ``timeout_s`` ueberschritten wird.
    """
    assert_paper_account(account_id)
    if isinstance(target, str):
        targets = {target.lower()}
    else:
        targets = {t.lower() for t in target}
    deadline = time.monotonic() + timeout_s
    last_status: str | None = None
    while time.monotonic() < deadline:
        response = await client.get(f"/v1/orders/{order_id}")
        if response.status_code == 200:
            body = response.json()
            status = (
                body.get("status")
                if isinstance(body, dict)
                else None
            )
            if status:
                last_status = status
                if status.lower() in targets:
                    return status
        await asyncio.sleep(poll_interval_s)
    raise TimeoutError(
        f"wait_for_order_status: order {order_id} erreicht "
        f"{sorted(targets)} nicht in {timeout_s}s "
        f"(letzter Status: {last_status!r})"
    )


# ---------------------------------------------------------------------------
# Klassen-DSL (aus K2 - bequemer Wrapper)
# ---------------------------------------------------------------------------


class PaperActions:
    """Vorgeladenes DSL-Objekt mit Account- und Notional-Tracking."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        account_id: str,
    ) -> None:
        self._client = client
        self._account_id = assert_paper_account(account_id)
        self._cumulative_notional = Decimal("0")

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def cumulative_notional_usd(self) -> Decimal:
        return self._cumulative_notional

    async def snapshot(
        self,
        *,
        conids: list[int],
        fields: list[str] | None = None,
    ) -> httpx.Response:
        params = {"conids": ",".join(str(c) for c in conids)}
        if fields:
            params["fields"] = ",".join(fields)
        return await self._client.get("/v1/quotes/snapshot", params=params)

    async def get_order(self, order_id: int | str) -> httpx.Response:
        return await self._client.get(f"/v1/orders/{order_id}")

    async def get_trades(self) -> httpx.Response:
        return await self._client.get("/v1/trades")

    async def get_portfolio_summary(self) -> httpx.Response:
        return await self._client.get(
            f"/v1/portfolio/{self._account_id}/summary"
        )

    async def place_order(
        self,
        *,
        conid: int,
        side: str,
        quantity: Decimal | int | str,
        limit_price: Decimal | int | str,
        time_in_force: str = "DAY",
    ) -> httpx.Response:
        if os.environ.get("BG_PAPER_TESTS_DISABLED", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            raise PaperSafetyError(
                "BG_PAPER_TESTS_DISABLED gesetzt - Schreib-Operation "
                "wird abgelehnt."
            )
        notional = Decimal(str(quantity)) * Decimal(str(limit_price))
        cumulative = self._cumulative_notional + notional
        assert_within_paper_limits(
            notional_usd=notional,
            cumulative_notional_usd=cumulative,
        )
        body: dict[str, Any] = {
            "account": self._account_id,
            "conid": conid,
            "side": side.upper(),
            "quantity": str(quantity),
            "order_type": "LMT",
            "limit_price": str(limit_price),
            "time_in_force": time_in_force.upper(),
        }
        response = await self._client.post("/v1/orders", json=body)
        if 200 <= response.status_code < 300:
            self._cumulative_notional = cumulative
        return response

    async def cancel_order(self, order_id: int | str) -> httpx.Response:
        return await self._client.delete(f"/v1/orders/{order_id}")


__all__ = [
    "PaperActions",
    "cancel_all_open_orders",
    "cancel_order",
    "flatten_positions",
    "place_limit_far_from_market",
    "subscribe_quote_stream",
    "wait_for_order_status",
]
