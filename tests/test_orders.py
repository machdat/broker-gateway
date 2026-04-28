"""Tests fuer Order-Models, OrdersService, Idempotency-Cache und /v1/orders."""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_ORDERS_WRITE,
    SCOPE_QUOTES_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore, generate_token_value
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle, AuthStatus
from broker_gateway.cp.orders import OrdersService
from broker_gateway.cp.portfolio import PortfolioService
from broker_gateway.idempotency import IdempotencyStore
from broker_gateway.main import create_app
from broker_gateway.order_models import (
    Order,
    OrderRequest,
    OrderSide,
    OrderType,
    TimeInForce,
)


_ADMIN_VALUE = "orders-admin-token-aaaaaaaaaaaaaa"
_ACCOUNT_ID = "U25235077"
_CONID = 265598


@pytest.fixture
def store() -> InMemoryTokenStore:
    s = InMemoryTokenStore()
    s.put(Token(value=_ADMIN_VALUE, caller_id="bootstrap-admin", scopes=[SCOPE_ADMIN_ALL]))
    return s


@pytest.fixture
async def cp_client(cp_gateway_mock) -> CPGatewayClient:
    client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    yield client
    await client.aclose()


@pytest.fixture
async def lifecycle(cp_client: CPGatewayClient) -> AuthLifecycle:
    lc = AuthLifecycle(cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0)
    yield lc
    await lc.stop()


@pytest.fixture
def orders(cp_client: CPGatewayClient) -> OrdersService:
    return OrdersService(cp_client)


@pytest.fixture
def portfolio(cp_client: CPGatewayClient) -> PortfolioService:
    return PortfolioService(cp_client, ttl_seconds=300.0)


@pytest.fixture
def idempotency() -> IdempotencyStore:
    return IdempotencyStore(ttl_seconds=300.0)


@pytest.fixture
async def client(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    orders: OrdersService,
    portfolio: PortfolioService,
    idempotency: IdempotencyStore,
    cp_gateway_mock,
):
    application = create_app(
        store=store,
        lifecycle=lifecycle,
        orders_service=orders,
        portfolio_service=portfolio,
        idempotency_store=idempotency,
    )
    with TestClient(application) as test_client:
        yield test_client


def _valid_payload() -> dict:
    return {
        "account_id": _ACCOUNT_ID,
        "conid": _CONID,
        "side": "BUY",
        "quantity": "1",
        "order_type": "LMT",
        "limit_price": "150.00",
        "tif": "DAY",
    }


# ---- OrderRequest Pydantic-Validierung ----


def test_order_request_lmt_requires_limit_price() -> None:
    with pytest.raises(ValueError):
        OrderRequest(
            account_id=_ACCOUNT_ID,
            conid=_CONID,
            side=OrderSide.BUY,
            quantity="1",
            order_type=OrderType.LMT,
        )


def test_order_request_stp_lmt_requires_both_prices() -> None:
    with pytest.raises(ValueError):
        OrderRequest(
            account_id=_ACCOUNT_ID,
            conid=_CONID,
            side=OrderSide.BUY,
            quantity="1",
            order_type=OrderType.STP_LMT,
            stop_price="100",
        )


def test_order_request_mkt_must_not_carry_prices() -> None:
    with pytest.raises(ValueError):
        OrderRequest(
            account_id=_ACCOUNT_ID,
            conid=_CONID,
            side=OrderSide.BUY,
            quantity="1",
            order_type=OrderType.MKT,
            limit_price="100",
        )


def test_order_request_quantity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        OrderRequest(
            account_id=_ACCOUNT_ID,
            conid=_CONID,
            side=OrderSide.BUY,
            quantity="0",
            order_type=OrderType.MKT,
        )


# ---- IdempotencyStore ----


def test_idempotency_get_returns_none_when_unset(idempotency: IdempotencyStore) -> None:
    assert idempotency.get("foo") is None


def test_idempotency_put_then_get(idempotency: IdempotencyStore) -> None:
    idempotency.put("foo", 201, {"x": 1})
    assert idempotency.get("foo") == (201, {"x": 1})


# ---- OrdersService ----


async def test_place_order_returns_order(orders: OrdersService) -> None:
    order = await orders.place_order(
        OrderRequest(
            account_id=_ACCOUNT_ID,
            conid=_CONID,
            side=OrderSide.BUY,
            quantity="1",
            order_type=OrderType.LMT,
            limit_price="150.00",
        )
    )
    assert isinstance(order, Order)
    assert order.account_id == _ACCOUNT_ID
    assert order.conid == _CONID
    assert order.status.value == "PendingSubmit"


async def test_place_order_handles_reply_confirmation_loop(
    orders: OrdersService, cp_gateway_mock
) -> None:
    cp_gateway_mock.reply_warnings = [
        {"id": "warn-1", "message": ["o-priceConstraint"]},
        {"id": "warn-2", "message": ["o-marginCheck"]},
    ]
    order = await orders.place_order(
        OrderRequest(
            account_id=_ACCOUNT_ID,
            conid=_CONID,
            side=OrderSide.BUY,
            quantity="1",
            order_type=OrderType.MKT,
        )
    )
    assert isinstance(order, Order)
    assert "o-priceConstraint" in order.warnings
    assert "o-marginCheck" in order.warnings


async def test_get_order_returns_money_for_filled(
    orders: OrdersService, cp_gateway_mock
) -> None:
    placed = await orders.place_order(
        OrderRequest(
            account_id=_ACCOUNT_ID,
            conid=_CONID,
            side=OrderSide.BUY,
            quantity="1",
            order_type=OrderType.LMT,
            limit_price="150.00",
        )
    )
    # Mock-Lifecycle: PendingSubmit -> Submitted -> Filled.
    await orders.get_order(placed.order_id)
    filled = await orders.get_order(placed.order_id)
    assert filled.status.value == "Filled"
    assert filled.avg_fill_price is not None
    assert filled.avg_fill_price.currency == "USD"
    assert filled.commission is not None
    assert Decimal(filled.commission.value) == Decimal("1.00")


async def test_cancel_order_returns_cancellation(
    orders: OrdersService,
) -> None:
    placed = await orders.place_order(
        OrderRequest(
            account_id=_ACCOUNT_ID,
            conid=_CONID,
            side=OrderSide.BUY,
            quantity="1",
            order_type=OrderType.MKT,
        )
    )
    cancellation = await orders.cancel_order(_ACCOUNT_ID, placed.order_id)
    assert cancellation.order_id == placed.order_id
    assert cancellation.status.value == "Cancelled"


async def test_get_order_uses_singular_status_path() -> None:
    """Belegt: cp/orders.py ruft den IBKR-Singular-Pfad
    /iserver/account/order/status/{orderId}, nicht den frueheren Bulk-
    Pfad. Quelle: docs/research/ibkr-cpapi-doc.json."""
    base_url = "http://cpgateway:5000/v1/api"
    order_id = "1234567890"
    with respx.mock(assert_all_called=False) as router:
        route = router.get(
            url=f"{base_url}/iserver/account/order/status/{order_id}"
        ).mock(
            return_value=httpx.Response(
                200,
                json={
                    "order_id": order_id,
                    "order_status": "Submitted",
                    "conid": 265598,
                    "side": "BUY",
                    "size": "1",
                    "order_type": "LMT",
                    "tif": "DAY",
                    "currency": "USD",
                    "limit_price": "150.00",
                    "stop_price": None,
                    "account_id": "U25235077",
                },
            )
        )
        client = CPGatewayClient(base_url=base_url)
        try:
            service = OrdersService(client)
            order = await service.get_order(order_id)
        finally:
            await client.aclose()
    assert route.called
    assert order.order_id == order_id
    assert order.status.value == "Submitted"


# ---- POST /v1/orders ----


def test_place_endpoint_requires_idempotency_key(client: TestClient) -> None:
    response = client.post(
        "/v1/orders",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
        json=_valid_payload(),
    )
    assert response.status_code == 400


def test_place_endpoint_returns_201(client: TestClient) -> None:
    response = client.post(
        "/v1/orders",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-place-1",
        },
        json=_valid_payload(),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["account_id"] == _ACCOUNT_ID
    assert body["status"] == "PendingSubmit"


def test_place_endpoint_replays_with_same_key(client: TestClient) -> None:
    headers = {
        "Authorization": f"Bearer {_ADMIN_VALUE}",
        "Idempotency-Key": "key-replay-1",
    }
    first = client.post("/v1/orders", headers=headers, json=_valid_payload())
    assert first.status_code == 201
    second = client.post("/v1/orders", headers=headers, json=_valid_payload())
    # Replay-Marker: 200 statt 201, gleicher Body.
    assert second.status_code == 200
    assert second.json() == first.json()


def test_place_endpoint_replay_does_not_call_cp_again(
    client: TestClient, cp_gateway_mock
) -> None:
    headers = {
        "Authorization": f"Bearer {_ADMIN_VALUE}",
        "Idempotency-Key": "key-replay-no-cp",
    }
    client.post("/v1/orders", headers=headers, json=_valid_payload())
    before = cp_gateway_mock.request_count
    client.post("/v1/orders", headers=headers, json=_valid_payload())
    assert cp_gateway_mock.request_count == before


def test_place_endpoint_invalid_order_type_returns_422(client: TestClient) -> None:
    payload = _valid_payload()
    payload["order_type"] = "BRACKET"
    response = client.post(
        "/v1/orders",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-invalid-type",
        },
        json=payload,
    )
    assert response.status_code == 422


def test_place_endpoint_lmt_without_limit_price_returns_422(client: TestClient) -> None:
    payload = _valid_payload()
    payload.pop("limit_price")
    response = client.post(
        "/v1/orders",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-lmt-no-price",
        },
        json=payload,
    )
    assert response.status_code == 422


def test_place_endpoint_without_token_returns_401(client: TestClient) -> None:
    response = client.post(
        "/v1/orders",
        headers={"Idempotency-Key": "key-no-token"},
        json=_valid_payload(),
    )
    assert response.status_code == 401


def test_place_endpoint_with_wrong_scope_returns_403(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    bad = generate_token_value()
    store.put(Token(value=bad, caller_id="psm", scopes=[SCOPE_QUOTES_READ]))
    response = client.post(
        "/v1/orders",
        headers={"Authorization": f"Bearer {bad}", "Idempotency-Key": "key-scope"},
        json=_valid_payload(),
    )
    assert response.status_code == 403


def test_place_endpoint_with_correct_scope(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    ok = generate_token_value()
    store.put(Token(value=ok, caller_id="trading-robot", scopes=[SCOPE_ORDERS_WRITE]))
    response = client.post(
        "/v1/orders",
        headers={"Authorization": f"Bearer {ok}", "Idempotency-Key": "key-scope-ok"},
        json=_valid_payload(),
    )
    assert response.status_code == 201


# ---- GET /v1/orders/{order_id} ----


def test_get_endpoint_returns_status(client: TestClient) -> None:
    place = client.post(
        "/v1/orders",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-get-1",
        },
        json=_valid_payload(),
    )
    order_id = place.json()["order_id"]
    response = client.get(
        f"/v1/orders/{order_id}",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == order_id


def test_get_endpoint_money_when_filled(client: TestClient) -> None:
    place = client.post(
        "/v1/orders",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-get-money",
        },
        json=_valid_payload(),
    )
    order_id = place.json()["order_id"]
    # Im Mock: erster GET -> Submitted, zweiter -> Filled (Money im Body).
    client.get(
        f"/v1/orders/{order_id}",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    response = client.get(
        f"/v1/orders/{order_id}",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    body = response.json()
    assert body["status"] == "Filled"
    assert body["commission"] is not None
    assert body["commission"]["currency"] == "USD"


# ---- DELETE /v1/orders/{order_id} ----


def test_cancel_endpoint_requires_idempotency_key(client: TestClient) -> None:
    place = client.post(
        "/v1/orders",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-cancel-prep",
        },
        json=_valid_payload(),
    )
    order_id = place.json()["order_id"]
    response = client.delete(
        f"/v1/orders/{order_id}",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "X-Account-Id": _ACCOUNT_ID,
        },
    )
    assert response.status_code == 400


def test_cancel_endpoint_requires_account_id(client: TestClient) -> None:
    place = client.post(
        "/v1/orders",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-cancel-prep-2",
        },
        json=_valid_payload(),
    )
    order_id = place.json()["order_id"]
    response = client.delete(
        f"/v1/orders/{order_id}",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-cancel-no-acct",
        },
    )
    assert response.status_code == 400


def test_cancel_endpoint_cancels(client: TestClient) -> None:
    place = client.post(
        "/v1/orders",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-cancel-prep-3",
        },
        json=_valid_payload(),
    )
    order_id = place.json()["order_id"]
    response = client.delete(
        f"/v1/orders/{order_id}",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-cancel-1",
            "X-Account-Id": _ACCOUNT_ID,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["order_id"] == order_id
    assert body["status"] == "Cancelled"


def test_cancel_endpoint_replay_returns_200(client: TestClient) -> None:
    place = client.post(
        "/v1/orders",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-cancel-prep-4",
        },
        json=_valid_payload(),
    )
    order_id = place.json()["order_id"]
    headers = {
        "Authorization": f"Bearer {_ADMIN_VALUE}",
        "Idempotency-Key": "key-cancel-replay",
        "X-Account-Id": _ACCOUNT_ID,
    }
    first = client.delete(f"/v1/orders/{order_id}", headers=headers)
    second = client.delete(f"/v1/orders/{order_id}", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()


# ---- Auth-Loss + Portfolio-Cache-Invalidation ----


async def test_place_endpoint_returns_503_when_session_lost(
    store: InMemoryTokenStore,
    orders: OrdersService,
    portfolio: PortfolioService,
    idempotency: IdempotencyStore,
    cp_gateway_mock,
) -> None:
    cp_client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    try:
        cp_gateway_mock.auth_lost = True
        lifecycle = AuthLifecycle(
            cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0
        )
        await lifecycle.tick_once()
        assert lifecycle.status is AuthStatus.AUTH_LOST

        application = create_app(
            store=store,
            lifecycle=lifecycle,
            orders_service=orders,
            portfolio_service=portfolio,
            idempotency_store=idempotency,
        )
        with TestClient(application) as test_client:
            response = test_client.post(
                "/v1/orders",
                headers={
                    "Authorization": f"Bearer {_ADMIN_VALUE}",
                    "Idempotency-Key": "key-503",
                },
                json=_valid_payload(),
            )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "30"
        await lifecycle.stop()
    finally:
        await cp_client.aclose()


def test_place_endpoint_invalidates_portfolio_cache(
    client: TestClient, portfolio: PortfolioService, cp_gateway_mock
) -> None:
    # Cache vorwaermen.
    import asyncio

    asyncio.get_event_loop().run_until_complete(portfolio.positions(_ACCOUNT_ID))
    assert len(portfolio._positions_cache) == 1

    response = client.post(
        "/v1/orders",
        headers={
            "Authorization": f"Bearer {_ADMIN_VALUE}",
            "Idempotency-Key": "key-invalidate-pf",
        },
        json=_valid_payload(),
    )
    assert response.status_code == 201
    # Place hat invalidate(_ACCOUNT_ID) gerufen -> Cache leer.
    assert len(portfolio._positions_cache) == 0
