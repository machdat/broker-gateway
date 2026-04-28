"""Order-Adapter gegen das CP-Gateway.

Kapselt die IBKR-Eigenheit, dass `POST /iserver/account/{acct}/orders`
nicht garantiert eine Order zurueckliefert: oft kommen erst Warning-
Confirmations (`[{id, message: [...]}]`), die der Caller per
`POST /iserver/reply/{id}` mit `{"confirmed": true}` quittieren muss,
bis im naechsten Reply die echte Order erscheint
(`[{order_id, order_status, ...}]`).

Aus Service-Sicht soll dieser Loop transparent sein: die HTTP-API
exponiert ein einziges `POST /v1/orders`, der Adapter sammelt
Warnings und quittiert sie automatisch (max. `MAX_REPLY_ROUNDS`).
Wer die unbestaetigten Warnings im Output sehen will, findet sie als
`warnings`-Liste im Order-Objekt.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status

from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.money import normalize_money
from broker_gateway.order_models import (
    Order,
    OrderCancellation,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)


logger = logging.getLogger(__name__)


_MAX_REPLY_ROUNDS = 5
_FALLBACK_CURRENCY = "USD"


class OrdersService:
    def __init__(self, client: CPGatewayClient) -> None:
        self._client = client

    async def place_order(self, request: OrderRequest) -> Order:
        """Place + automatischer Reply-Confirmation-Loop.

        Antwortet das CP-Gateway mit Warnings (Schema
        `[{id, message: [...]}]`), quittiert der Adapter sie iterativ
        per `POST /iserver/reply/{id}` mit `{"confirmed": true}`. Nach
        spaetestens `_MAX_REPLY_ROUNDS` Iterationen bricht er mit 502
        ab.
        """

        body = _request_to_cp_body(request)
        path = f"/iserver/account/{request.account_id}/orders"
        warnings: list[str] = []

        response = await self._client.post(path, json={"orders": [body]})
        for _ in range(_MAX_REPLY_ROUNDS):
            if response.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"CP-Gateway-Fehler bei place_order: HTTP {response.status_code}",
                )
            payload = response.json()
            if not isinstance(payload, list) or not payload:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="CP-Gateway lieferte unerwartetes Schema bei place_order",
                )
            entry = payload[0]
            if "order_id" in entry:
                return _entry_to_order(entry, request, warnings)
            reply_id = entry.get("id")
            if not reply_id:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="CP-Gateway lieferte weder order_id noch reply-id",
                )
            warnings.extend(_extract_messages(entry))
            response = await self._client.post(
                f"/iserver/reply/{reply_id}",
                json={"confirmed": True},
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Reply-Confirmation-Loop nach {_MAX_REPLY_ROUNDS} Runden ohne Order-Resultat",
        )

    async def get_order(self, order_id: str) -> Order:
        # IBKR liefert Status nur ueber Singular-Pfad. Bulk-Endpoint
        # /iserver/account/orders/{order_id} existiert in der CP-API nicht.
        # Quelle: docs/research/ibkr-cpapi-doc.json, Pfad
        # /iserver/account/order/status/{orderId}.
        response = await self._client.get(f"/iserver/account/order/status/{order_id}")
        if response.status_code == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order_id unbekannt")
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"CP-Gateway-Fehler bei get_order: HTTP {response.status_code}",
            )
        return _entry_to_order(response.json(), None, [])

    async def cancel_order(self, account_id: str, order_id: str) -> OrderCancellation:
        response = await self._client.delete(
            f"/iserver/account/{account_id}/order/{order_id}"
        )
        if response.status_code == 404:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="order_id unbekannt")
        if response.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"CP-Gateway-Fehler bei cancel_order: HTTP {response.status_code}",
            )
        return OrderCancellation(
            order_id=order_id,
            cancelled_at=datetime.now(timezone.utc),
        )


def _request_to_cp_body(req: OrderRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "conid": req.conid,
        "orderType": req.order_type.value,
        "side": req.side.value,
        "tif": req.tif.value,
        "quantity": float(req.quantity),
    }
    if req.limit_price is not None:
        body["price"] = float(req.limit_price)
    if req.stop_price is not None:
        body["auxPrice"] = float(req.stop_price)
    return body


def _extract_messages(entry: dict[str, Any]) -> list[str]:
    raw = entry.get("message")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    if isinstance(raw, str) and raw:
        return [raw]
    return []


def _entry_to_order(
    entry: dict[str, Any],
    request: OrderRequest | None,
    warnings: list[str],
) -> Order:
    raw_status = entry.get("order_status") or entry.get("status") or "Submitted"
    try:
        order_status = OrderStatus(raw_status)
    except ValueError:
        order_status = OrderStatus.SUBMITTED

    side_raw = (entry.get("side") or (request.side.value if request else "BUY")).upper()
    try:
        side = OrderSide(side_raw)
    except ValueError:
        side = request.side if request else OrderSide.BUY

    type_raw = (entry.get("order_type") or (request.order_type.value if request else "MKT")).upper()
    try:
        order_type = OrderType(type_raw)
    except ValueError:
        order_type = request.order_type if request else OrderType.MKT

    tif_raw = (entry.get("tif") or (request.tif.value if request else "DAY")).upper()
    try:
        tif = TimeInForce(tif_raw)
    except ValueError:
        tif = request.tif if request else TimeInForce.DAY

    quantity = entry.get("size") or entry.get("quantity")
    if quantity is None and request is not None:
        quantity = request.quantity
    quantity_str = str(quantity) if quantity is not None else "0"

    currency = entry.get("currency") or _FALLBACK_CURRENCY

    avg_fill_price = normalize_money(entry.get("avg_price"), currency)
    commission = normalize_money(entry.get("commission"), currency)
    filled_quantity = entry.get("filled_quantity")
    filled_quantity_str = str(filled_quantity) if filled_quantity is not None else None

    limit_price = entry.get("limit_price")
    stop_price = entry.get("stop_price")
    if request is not None:
        limit_price = limit_price if limit_price is not None else request.limit_price
        stop_price = stop_price if stop_price is not None else request.stop_price

    account_id = entry.get("account_id") or (request.account_id if request else "")
    conid = entry.get("conid") or (request.conid if request else 0)

    return Order(
        order_id=str(entry["order_id"]),
        account_id=str(account_id),
        conid=int(conid),
        side=side,
        quantity=quantity_str,
        order_type=order_type,
        tif=tif,
        status=order_status,
        limit_price=str(limit_price) if limit_price is not None else None,
        stop_price=str(stop_price) if stop_price is not None else None,
        avg_fill_price=avg_fill_price,
        commission=commission,
        filled_quantity=filled_quantity_str,
        warnings=list(warnings),
    )


__all__ = ["OrdersService"]
