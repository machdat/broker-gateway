"""POST /v1/orders, GET /v1/orders/{order_id}, DELETE /v1/orders/{order_id}.

Idempotency-Konvention:
- POST `/v1/orders` und DELETE `/v1/orders/{order_id}` erfordern den
  Header `Idempotency-Key`. Fehlt er, antwortet der Service mit
  `400 Bad Request`.
- Replay mit gleichem Key liefert die gespeicherte Antwort (200 statt
  201/202), ohne den CP-Gateway erneut zu treffen.

Out-of-Scope (Nachfolge-Karten): Smart-Routing, OCA-Brackets,
Preview/What-If, Trailing-Stops.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Response, status
from fastapi.encoders import jsonable_encoder

from broker_gateway.auth.middleware import require_any_scope, require_scope
from broker_gateway.auth.models import SCOPE_ORDERS_READ, SCOPE_ORDERS_WRITE, Token
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok
from broker_gateway.cp.orders import OrdersService
from broker_gateway.cp.portfolio import PortfolioService
from broker_gateway.idempotency import IdempotencyStore
from broker_gateway.order_models import (
    Order,
    OrderCancellation,
    OrderModifyRequest,
    OrderRequest,
    WhatIfPreview,
)


router = APIRouter(prefix="/orders", tags=["orders"])


def get_orders_service() -> OrdersService:
    raise RuntimeError(
        "get_orders_service muss in der App per dependency_overrides gesetzt werden"
    )


def get_idempotency_store() -> IdempotencyStore:
    raise RuntimeError(
        "get_idempotency_store muss in der App per dependency_overrides gesetzt werden"
    )


def get_orders_portfolio_invalidator() -> PortfolioService | None:
    """Optionaler Hook: PortfolioService-Cache nach Place/Cancel busten."""
    return None


def _require_idempotency_key(value: str | None) -> str:
    if value is None or not value.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Idempotency-Key-Header ist Pflicht",
        )
    return value.strip()


@router.post(
    "",
    response_model=Order,
    status_code=status.HTTP_201_CREATED,
    summary="Order platzieren (idempotent ueber Idempotency-Key)",
)
async def place_order(
    request: OrderRequest,
    response: Response,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    _scope: Annotated[Token, Depends(require_scope(SCOPE_ORDERS_WRITE))] = ...,
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)] = ...,
    service: Annotated[OrdersService, Depends(get_orders_service)] = ...,
    store: Annotated[IdempotencyStore, Depends(get_idempotency_store)] = ...,
    portfolio: Annotated[
        PortfolioService | None, Depends(get_orders_portfolio_invalidator)
    ] = None,
) -> Any:
    key = _require_idempotency_key(idempotency_key)
    cached = store.get(_scoped_key("POST", key))
    if cached is not None:
        cached_status, cached_payload = cached
        # Replay: Status 200, damit der Caller das vom Erst-201 unterscheiden kann.
        response.status_code = status.HTTP_200_OK
        return cached_payload

    order = await service.place_order(request)
    payload = jsonable_encoder(order)
    store.put(_scoped_key("POST", key), status.HTTP_201_CREATED, payload)

    if portfolio is not None:
        portfolio.invalidate(request.account_id)

    return payload


@router.get(
    "",
    response_model=list[Order],
    summary="Offene Orders auflisten (GTC-STP inkl. OCA-Gruppen)",
)
async def list_orders(
    _scope: Annotated[
        Token, Depends(require_any_scope(SCOPE_ORDERS_READ, SCOPE_ORDERS_WRITE))
    ],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[OrdersService, Depends(get_orders_service)],
    account_id: Annotated[
        str | None,
        Query(description="Optionaler Filter: nur offene Orders dieses Kontos"),
    ] = None,
) -> list[Order]:
    # Liefert die broker-seitige Sicht offener/aktiver Orders (inkl. GTC-STP
    # und OCA-Gruppen) als Wahrheitsquelle fuer Stop-Coverage/Reconciliation.
    # Scope-Semantik wie GET /{order_id}: orders:read genuegt.
    orders = await service.list_open()
    wanted = account_id.strip() if account_id else ""
    if wanted:
        orders = [o for o in orders if o.account_id == wanted]
    return orders


@router.post(
    "/whatif",
    response_model=WhatIfPreview,
    summary="What-If-/Margin-Vorschau (platziert nichts)",
)
async def whatif_order(
    request: OrderRequest,
    _scope: Annotated[
        Token, Depends(require_any_scope(SCOPE_ORDERS_READ, SCOPE_ORDERS_WRITE))
    ],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[OrdersService, Depends(get_orders_service)],
) -> WhatIfPreview:
    # Reine Vorschau - kein Idempotency-Key, keine Cache-Invalidation.
    # Scope-Semantik wie GET /{order_id}: orders:read genuegt, orders:write
    # schliesst das Leserecht mit ein (require_any_scope).
    return await service.whatif_order(request)


@router.get(
    "/{order_id}",
    response_model=Order,
    summary="Order-Status",
)
async def get_order(
    order_id: Annotated[str, Path(min_length=1)],
    _scope: Annotated[
        Token, Depends(require_any_scope(SCOPE_ORDERS_READ, SCOPE_ORDERS_WRITE))
    ],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[OrdersService, Depends(get_orders_service)],
) -> Order:
    return await service.get_order(order_id)


@router.delete(
    "/{order_id}",
    response_model=OrderCancellation,
    summary="Order canceln (idempotent ueber Idempotency-Key)",
)
async def cancel_order(
    order_id: Annotated[str, Path(min_length=1)],
    response: Response,
    account_id: Annotated[str | None, Header(alias="X-Account-Id")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    _scope: Annotated[Token, Depends(require_scope(SCOPE_ORDERS_WRITE))] = ...,
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)] = ...,
    service: Annotated[OrdersService, Depends(get_orders_service)] = ...,
    store: Annotated[IdempotencyStore, Depends(get_idempotency_store)] = ...,
    portfolio: Annotated[
        PortfolioService | None, Depends(get_orders_portfolio_invalidator)
    ] = None,
) -> Any:
    key = _require_idempotency_key(idempotency_key)
    if not account_id or not account_id.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Account-Id-Header ist Pflicht fuer Cancel",
        )
    account = account_id.strip()

    cached = store.get(_scoped_key("DELETE", key))
    if cached is not None:
        cached_status, cached_payload = cached
        response.status_code = status.HTTP_200_OK
        return cached_payload

    cancellation = await service.cancel_order(account, order_id)
    payload = jsonable_encoder(cancellation)
    store.put(_scoped_key("DELETE", key), status.HTTP_200_OK, payload)

    if portfolio is not None:
        portfolio.invalidate(account)

    return payload


@router.patch(
    "/{order_id}",
    response_model=Order,
    summary="Order modifizieren (cancel/replace, idempotent ueber Idempotency-Key)",
)
async def modify_order(
    request: OrderModifyRequest,
    response: Response,
    order_id: Annotated[str, Path(min_length=1)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    _scope: Annotated[Token, Depends(require_scope(SCOPE_ORDERS_WRITE))] = ...,
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)] = ...,
    service: Annotated[OrdersService, Depends(get_orders_service)] = ...,
    store: Annotated[IdempotencyStore, Depends(get_idempotency_store)] = ...,
    portfolio: Annotated[
        PortfolioService | None, Depends(get_orders_portfolio_invalidator)
    ] = None,
) -> Any:
    # account_id kommt aus dem Body (OrderModifyRequest), nicht aus einem
    # Header - konsistent mit POST (place_order). Stop-/Limit-Level oder
    # Menge werden auf die bestehende Order angewandt (IBKR cancel/replace).
    key = _require_idempotency_key(idempotency_key)
    cached = store.get(_scoped_key("PATCH", key))
    if cached is not None:
        cached_status, cached_payload = cached
        response.status_code = status.HTTP_200_OK
        return cached_payload

    order = await service.modify_order(request.account_id, order_id, request)
    payload = jsonable_encoder(order)
    store.put(_scoped_key("PATCH", key), status.HTTP_200_OK, payload)

    if portfolio is not None:
        portfolio.invalidate(request.account_id)

    return payload


def _scoped_key(method: str, key: str) -> str:
    """Idempotency-Key wird pro Methode gescoped, damit ein und derselbe
    Key auf POST und DELETE nicht kollidiert."""
    return f"{method}:{key}"
