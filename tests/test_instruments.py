"""Tests fuer InstrumentsService und /v1/instruments-Endpunkte."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_INSTRUMENTS_READ,
    SCOPE_QUOTES_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore, generate_token_value
from broker_gateway.cache import TTLCache
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.instruments import InstrumentsService
from broker_gateway.cp.lifecycle import AuthLifecycle, AuthStatus
from broker_gateway.main import create_app


_ADMIN_VALUE = "instruments-admin-token-aaaaaaaaaaaaaa"


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
def instruments(cp_client: CPGatewayClient) -> InstrumentsService:
    return InstrumentsService(cp_client, ttl_seconds=300.0)


@pytest.fixture
async def client(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    instruments: InstrumentsService,
    cp_gateway_mock,
):
    application = create_app(store=store, lifecycle=lifecycle, instruments_service=instruments)
    with TestClient(application) as test_client:
        yield test_client


# ---- TTLCache ----

def test_ttl_cache_hit_and_miss() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl_seconds=60.0)
    assert cache.get("a") == (None, False)
    cache.set("a", 42)
    assert cache.get("a") == (42, True)


def test_ttl_cache_expires_lazy() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl_seconds=0.01)
    cache.set("a", 1)
    import time
    time.sleep(0.02)
    assert cache.get("a") == (None, False)
    assert len(cache) == 0


def test_ttl_cache_invalidate() -> None:
    cache: TTLCache[str, int] = TTLCache(ttl_seconds=60.0)
    cache.set("a", 1)
    assert cache.invalidate("a") is True
    assert cache.invalidate("a") is False


# ---- InstrumentsService ----

async def test_search_returns_instruments_from_cp(
    instruments: InstrumentsService, cp_gateway_mock
) -> None:
    result = await instruments.search("AAPL")
    assert len(result) == 1
    assert result[0].conid == 265598
    assert result[0].symbol == "AAPL"


async def test_search_caches_subsequent_calls(
    instruments: InstrumentsService, cp_gateway_mock
) -> None:
    await instruments.search("AAPL")
    before = cp_gateway_mock.request_count
    await instruments.search("AAPL")
    await instruments.search("AAPL")
    assert cp_gateway_mock.request_count == before


async def test_search_unknown_symbol_returns_empty_and_caches(
    instruments: InstrumentsService, cp_gateway_mock
) -> None:
    result = await instruments.search("ZZZZ")
    assert result == []
    before = cp_gateway_mock.request_count
    await instruments.search("ZZZZ")
    assert cp_gateway_mock.request_count == before


async def test_info_returns_detail_and_caches(
    instruments: InstrumentsService, cp_gateway_mock
) -> None:
    detail = await instruments.info(265598)
    assert detail.conid == 265598
    assert detail.symbol == "AAPL"
    # IBKR liefert in Realitaet "NASDAQ.NMS" (National Market System);
    # seed nutzt das vereinfachte "NASDAQ".
    assert detail.exchange.startswith("NASDAQ")

    before = cp_gateway_mock.request_count
    await instruments.info(265598)
    assert cp_gateway_mock.request_count == before


async def test_info_unknown_conid_raises_404(
    instruments: InstrumentsService, cp_gateway_mock
) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await instruments.info(999999)
    assert exc.value.status_code == 404


# ---- Endpunkte ----

async def test_search_endpoint_with_admin_token(client: TestClient) -> None:
    response = client.get(
        "/v1/instruments/search",
        params={"symbol": "AAPL"},
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["conid"] == 265598
    assert body[0]["symbol"] == "AAPL"


async def test_endpoint_without_token_returns_401(client: TestClient) -> None:
    response = client.get("/v1/instruments/search", params={"symbol": "AAPL"})
    assert response.status_code == 401


async def test_endpoint_with_wrong_scope_returns_403(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    wrong_value = generate_token_value()
    store.put(Token(value=wrong_value, caller_id="psm", scopes=[SCOPE_QUOTES_READ]))
    response = client.get(
        "/v1/instruments/search",
        params={"symbol": "AAPL"},
        headers={"Authorization": f"Bearer {wrong_value}"},
    )
    assert response.status_code == 403


async def test_endpoint_with_correct_scope(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    ok_value = generate_token_value()
    store.put(Token(value=ok_value, caller_id="psm", scopes=[SCOPE_INSTRUMENTS_READ]))
    response = client.get(
        "/v1/instruments/search",
        params={"symbol": "MSFT"},
        headers={"Authorization": f"Bearer {ok_value}"},
    )
    assert response.status_code == 200
    assert response.json()[0]["symbol"] == "MSFT"


async def test_get_instrument_by_conid(client: TestClient) -> None:
    response = client.get(
        "/v1/instruments/265598",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["conid"] == 265598
    assert body["symbol"] == "AAPL"
    # Live: "NASDAQ.NMS", seed: "NASDAQ" - beide akzeptieren.
    assert body["exchange"].startswith("NASDAQ")


async def test_get_instrument_unknown_returns_404(client: TestClient) -> None:
    response = client.get(
        "/v1/instruments/123456789",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 404


async def test_endpoint_returns_503_when_session_lost(
    store: InMemoryTokenStore,
    instruments: InstrumentsService,
    cp_gateway_mock,
) -> None:
    cp_client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    try:
        cp_gateway_mock.auth_lost = True
        lifecycle = AuthLifecycle(cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0)
        await lifecycle.tick_once()
        assert lifecycle.status is AuthStatus.AUTH_LOST

        application = create_app(store=store, lifecycle=lifecycle, instruments_service=instruments)
        with TestClient(application) as test_client:
            response = test_client.get(
                "/v1/instruments/search",
                params={"symbol": "AAPL"},
                headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
            )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "30"
        await lifecycle.stop()
    finally:
        await cp_client.aclose()
