"""Tests fuer Availability-Mapping, QuotesService.snapshot_with_prime
und GET /v1/quotes/snapshot.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from broker_gateway.availability import map_availability
from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_PORTFOLIO_READ,
    SCOPE_QUOTES_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore, generate_token_value
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle, AuthStatus
from broker_gateway.cp.quotes import (
    FIELD_ALIASES,
    QuotesService,
    resolve_fields,
)
from broker_gateway.main import create_app


_ADMIN_VALUE = "quotes-admin-token-aaaaaaaaaaaaaaaaa"


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
def quotes(cp_client: CPGatewayClient) -> QuotesService:
    # prime_delay_s=0 macht Tests schnell - das CP-Mock antwortet sofort,
    # die Wartezeit ist im Production-Code nur für echte CP-Gateways nötig.
    return QuotesService(cp_client, prime_delay_s=0.0)


@pytest.fixture
async def client(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    quotes: QuotesService,
    cp_gateway_mock,
):
    application = create_app(
        store=store,
        lifecycle=lifecycle,
        quotes_service=quotes,
    )
    with TestClient(application) as test_client:
        yield test_client


# ---- map_availability ----

@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("RPB", "realtime"),
        ("DPB", "delayed"),
        ("FPB", "frozen"),
        ("RNS", "realtime"),
        ("DXX", "delayed"),
        ("Frozen", "frozen"),
        ("rpb", "realtime"),
    ],
)
def test_map_availability_known_codes(code: str, expected: str) -> None:
    assert map_availability(code) == expected


@pytest.mark.parametrize("code", ["", None, "Z", "XPB"])
def test_map_availability_unknown_returns_none(code: str | None) -> None:
    assert map_availability(code) is None


# ---- resolve_fields ----

def test_resolve_fields_maps_aliases_to_codes() -> None:
    assert resolve_fields(["last", "bid", "ask"]) == [FIELD_ALIASES["last"], FIELD_ALIASES["bid"], FIELD_ALIASES["ask"]]


def test_resolve_fields_dedupes() -> None:
    result = resolve_fields(["last", "last", "bid"])
    assert result == [FIELD_ALIASES["last"], FIELD_ALIASES["bid"]]


def test_resolve_fields_unknown_raises_422() -> None:
    with pytest.raises(HTTPException) as exc:
        resolve_fields(["last", "definitely_not_a_field"])
    assert exc.value.status_code == 422


# ---- QuotesService.snapshot_with_prime ----

async def test_snapshot_with_prime_delivers_values_despite_empty_first_call(
    quotes: QuotesService, cp_gateway_mock
) -> None:
    result = await quotes.snapshot_with_prime([265598], [FIELD_ALIASES["last"], FIELD_ALIASES["bid"]])
    assert len(result) == 1
    quote = result[0]
    assert quote.conid == 265598
    # Die Mock-Fixture liefert beim ersten Call leer und beim zweiten die Werte.
    # Service nimmt den zweiten Call.
    assert quote.last == "150.50"
    assert quote.bid == "150.45"


async def test_snapshot_with_prime_includes_availability_normalised(
    quotes: QuotesService, cp_gateway_mock
) -> None:
    result = await quotes.snapshot_with_prime([265598], [FIELD_ALIASES["last"]])
    quote = result[0]
    assert quote.availability == "delayed"
    assert quote.availability_raw == "DPB"


async def test_snapshot_with_prime_does_two_calls(
    quotes: QuotesService, cp_gateway_mock
) -> None:
    before = cp_gateway_mock.request_count
    await quotes.snapshot_with_prime([265598], [FIELD_ALIASES["last"]])
    after = cp_gateway_mock.request_count
    # First-Call + Second-Call.
    assert after - before == 2


async def test_snapshot_with_prime_handles_multiple_conids(
    quotes: QuotesService, cp_gateway_mock
) -> None:
    result = await quotes.snapshot_with_prime(
        [265598, 272093],
        [FIELD_ALIASES["last"], FIELD_ALIASES["ask"]],
    )
    assert {q.conid for q in result} == {265598, 272093}


# ---- /v1/quotes/snapshot ----

async def test_snapshot_endpoint_with_admin_token(client: TestClient) -> None:
    response = client.get(
        "/v1/quotes/snapshot",
        params={"conids": "265598", "fields": "last,bid,ask,availability"},
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["conid"] == 265598
    assert body[0]["last"] == "150.50"
    assert body[0]["availability"] == "delayed"
    assert body[0]["availability_raw"] == "DPB"


async def test_snapshot_endpoint_default_fields(client: TestClient) -> None:
    response = client.get(
        "/v1/quotes/snapshot",
        params={"conids": "265598"},
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body[0]["last"] is not None
    assert body[0]["availability"] == "delayed"


async def test_snapshot_endpoint_too_many_conids_returns_422(client: TestClient) -> None:
    response = client.get(
        "/v1/quotes/snapshot",
        params={"conids": "1,2,3,4,5,6"},
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 422
    assert "Maximal 5" in response.json()["detail"]


async def test_snapshot_endpoint_unknown_field_returns_422(client: TestClient) -> None:
    response = client.get(
        "/v1/quotes/snapshot",
        params={"conids": "265598", "fields": "last,foobar"},
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 422


async def test_snapshot_endpoint_invalid_conid_returns_422(client: TestClient) -> None:
    response = client.get(
        "/v1/quotes/snapshot",
        params={"conids": "abc"},
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 422


async def test_snapshot_endpoint_without_token_returns_401(client: TestClient) -> None:
    response = client.get("/v1/quotes/snapshot", params={"conids": "265598"})
    assert response.status_code == 401


async def test_snapshot_endpoint_with_wrong_scope_returns_403(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    bad = generate_token_value()
    store.put(Token(value=bad, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))
    response = client.get(
        "/v1/quotes/snapshot",
        params={"conids": "265598"},
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert response.status_code == 403


async def test_snapshot_endpoint_503_when_session_lost(
    store: InMemoryTokenStore,
    quotes: QuotesService,
    cp_gateway_mock,
) -> None:
    cp_client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    try:
        cp_gateway_mock.auth_lost = True
        lifecycle = AuthLifecycle(cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0)
        await lifecycle.tick_once()
        assert lifecycle.status is AuthStatus.AUTH_LOST

        application = create_app(store=store, lifecycle=lifecycle, quotes_service=quotes)
        with TestClient(application) as test_client:
            response = test_client.get(
                "/v1/quotes/snapshot",
                params={"conids": "265598"},
                headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
            )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "30"
        await lifecycle.stop()
    finally:
        await cp_client.aclose()


async def test_snapshot_endpoint_with_correct_scope(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    ok = generate_token_value()
    store.put(Token(value=ok, caller_id="psm", scopes=[SCOPE_QUOTES_READ]))
    response = client.get(
        "/v1/quotes/snapshot",
        params={"conids": "272093"},
        headers={"Authorization": f"Bearer {ok}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body[0]["conid"] == 272093
