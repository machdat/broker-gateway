"""Tests fuer Money, PortfolioService und /v1/portfolio-Endpunkte."""
from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from broker_gateway.auth.models import (
    SCOPE_ADMIN_ALL,
    SCOPE_PORTFOLIO_READ,
    SCOPE_QUOTES_READ,
    Token,
)
from broker_gateway.auth.store import InMemoryTokenStore, generate_token_value
from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.lifecycle import AuthLifecycle, AuthStatus
from broker_gateway.cp.portfolio import PortfolioService
from broker_gateway.main import create_app
from broker_gateway.money import Money, normalize_money, normalize_summary_money


_ADMIN_VALUE = "portfolio-admin-token-aaaaaaaaaaaaaaa"
_ACCOUNT_ID = "U25235077"


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
def portfolio(cp_client: CPGatewayClient) -> PortfolioService:
    return PortfolioService(cp_client, ttl_seconds=300.0)


@pytest.fixture
async def client(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    portfolio: PortfolioService,
    cp_gateway_mock,
):
    application = create_app(
        store=store,
        lifecycle=lifecycle,
        portfolio_service=portfolio,
    )
    with TestClient(application) as test_client:
        yield test_client


# ---- Money / normalize_money ----

def test_normalize_money_returns_none_for_missing_inputs() -> None:
    assert normalize_money(None, "USD") is None
    assert normalize_money(100.0, None) is None
    assert normalize_money(100.0, " ") is None


def test_normalize_money_keeps_decimal_precision() -> None:
    money = normalize_money("123.456", "usd")
    assert money is not None
    assert money.value == "123.456"
    assert money.currency == "USD"


def test_normalize_money_from_int_and_float() -> None:
    assert normalize_money(150, "EUR") == Money(value="150", currency="EUR")
    money = normalize_money(150.5, "eur")
    assert money is not None
    assert Decimal(money.value) == Decimal("150.5")


def test_normalize_money_rejects_bool() -> None:
    with pytest.raises(ValueError):
        normalize_money(True, "USD")


# ---- normalize_summary_money (IBKR /portfolio/{aid}/summary-Schema) ----

def test_normalize_summary_money_maps_amount_and_currency() -> None:
    field = {"amount": 12345.67, "currency": "EUR", "value": "12345.67", "isNull": False, "timestamp": 1700000000}
    money = normalize_summary_money(field)
    assert money is not None
    assert Decimal(money.value) == Decimal("12345.67")
    assert money.currency == "EUR"


def test_normalize_summary_money_returns_none_on_isnull() -> None:
    assert normalize_summary_money({"amount": 0.0, "currency": "USD", "isNull": True}) is None


def test_normalize_summary_money_returns_none_on_missing_fields() -> None:
    assert normalize_summary_money(None) is None
    assert normalize_summary_money({}) is None
    assert normalize_summary_money({"amount": 1.0}) is None
    assert normalize_summary_money({"currency": "USD"}) is None


# ---- PortfolioService ----

async def test_positions_returns_mapped_holdings(
    portfolio: PortfolioService, cp_gateway_mock
) -> None:
    positions = await portfolio.positions(_ACCOUNT_ID)
    # Live-Recording (v1.3.0) liefert echte Positionen des Accounts; Tests
    # pruefen Schema-Konformitaet, nicht harte Counts.
    assert len(positions) >= 1
    sample = positions[0]
    assert sample.account_id == _ACCOUNT_ID
    assert isinstance(sample.conid, int)
    # quantity ist Decimal-String
    Decimal(sample.quantity)
    if sample.market_price is not None:
        assert sample.market_price.currency


async def test_positions_caches_subsequent_calls(
    portfolio: PortfolioService, cp_gateway_mock
) -> None:
    await portfolio.positions(_ACCOUNT_ID)
    before = cp_gateway_mock.request_count
    await portfolio.positions(_ACCOUNT_ID)
    await portfolio.positions(_ACCOUNT_ID)
    assert cp_gateway_mock.request_count == before


async def test_invalidate_busts_caches(
    portfolio: PortfolioService, cp_gateway_mock
) -> None:
    await portfolio.positions(_ACCOUNT_ID)
    await portfolio.ledger(_ACCOUNT_ID)
    portfolio.invalidate(_ACCOUNT_ID)
    before = cp_gateway_mock.request_count
    await portfolio.positions(_ACCOUNT_ID)
    await portfolio.ledger(_ACCOUNT_ID)
    # ledger ist ein Call; positions paginiert bis Seite < pageSize 30 -
    # mindestens ein Call, mit dem Live-Recording (18 Positionen) genau einer.
    assert cp_gateway_mock.request_count - before >= 2


async def test_positions_paginates_until_short_page(
    portfolio: PortfolioService, cp_gateway_mock
) -> None:
    """positions() iteriert ueber pageId, bis eine Seite < pageSize liefert.

    Mit dem aktuellen Replay-Loader gibt es nur Seite 0; Seite 1 liefert
    [] ueber den LookupError-Fallback im Mock. Erwartung: wir erhalten
    eine nicht-leere Liste und der Mock hat genau 2 Requests gesehen
    (Seite 0 = nicht-voll bei Seed, Seite 0 = 18 Eintraege < 30 bei
    Live, in jedem Fall hoert es nach 1 Call auf).
    """
    portfolio.invalidate(_ACCOUNT_ID)
    before = cp_gateway_mock.request_count
    positions = await portfolio.positions(_ACCOUNT_ID)
    assert len(positions) >= 1
    # Page-Default-Size ist 30; wenn Seite 0 < 30 Eintraege liefert, ist
    # genau ein Request noetig.
    assert cp_gateway_mock.request_count - before == 1


async def test_ledger_returns_currency_normalised_entries(
    portfolio: PortfolioService, cp_gateway_mock
) -> None:
    ledger = await portfolio.ledger(_ACCOUNT_ID)
    currencies = {entry.currency for entry in ledger.entries}
    # IBKR liefert mind. eine "echte" Currency neben dem (verworfenen) BASE-Eintrag.
    assert currencies, "ledger sollte mindestens eine Currency enthalten"
    assert "BASE" not in currencies
    sample = ledger.entries[0]
    assert sample.currency == sample.currency.upper()


async def test_summary_uses_native_endpoint(
    portfolio: PortfolioService, cp_gateway_mock
) -> None:
    summary = await portfolio.summary(_ACCOUNT_ID)
    assert summary.account_id == _ACCOUNT_ID
    # Native Summary liefert echtes net_liquidation (nicht aus Aggregat).
    assert summary.net_liquidation is not None
    assert summary.base_currency == summary.net_liquidation.currency
    assert summary.position_count >= 1


async def test_summary_caches_and_does_not_repoll(
    portfolio: PortfolioService, cp_gateway_mock
) -> None:
    await portfolio.summary(_ACCOUNT_ID)
    before = cp_gateway_mock.request_count
    await portfolio.summary(_ACCOUNT_ID)
    assert cp_gateway_mock.request_count == before


# ---- Endpunkte ----

def test_summary_endpoint_with_admin_token(client: TestClient) -> None:
    response = client.get(
        f"/v1/portfolio/{_ACCOUNT_ID}",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == _ACCOUNT_ID
    assert body["position_count"] >= 1
    assert body["net_liquidation"] is not None


def test_positions_endpoint_returns_currency_objects(client: TestClient) -> None:
    response = client.get(
        f"/v1/portfolio/{_ACCOUNT_ID}/positions",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200
    positions = response.json()
    assert positions, "Account sollte mindestens eine Position haben"
    assert any(
        p.get("market_price") is not None and p["market_price"].get("currency")
        for p in positions
    )


def test_ledger_endpoint(client: TestClient) -> None:
    response = client.get(
        f"/v1/portfolio/{_ACCOUNT_ID}/ledger",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200
    body = response.json()
    currencies = {e["currency"] for e in body["entries"]}
    assert currencies, "ledger sollte mindestens eine Currency liefern"
    assert "BASE" not in currencies


def test_endpoint_without_token_returns_401(client: TestClient) -> None:
    response = client.get(f"/v1/portfolio/{_ACCOUNT_ID}")
    assert response.status_code == 401


def test_endpoint_with_wrong_scope_returns_403(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    bad = generate_token_value()
    store.put(Token(value=bad, caller_id="psm", scopes=[SCOPE_QUOTES_READ]))
    response = client.get(
        f"/v1/portfolio/{_ACCOUNT_ID}",
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert response.status_code == 403


def test_endpoint_with_correct_scope(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    ok = generate_token_value()
    store.put(Token(value=ok, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))
    response = client.get(
        f"/v1/portfolio/{_ACCOUNT_ID}/positions",
        headers={"Authorization": f"Bearer {ok}"},
    )
    assert response.status_code == 200


async def test_endpoint_returns_503_when_session_lost(
    store: InMemoryTokenStore,
    portfolio: PortfolioService,
    cp_gateway_mock,
) -> None:
    cp_client = CPGatewayClient(base_url=cp_gateway_mock.base_url)
    try:
        cp_gateway_mock.auth_lost = True
        lifecycle = AuthLifecycle(cp_client, tickle_interval_s=10.0, reauth_max_retries=1, reauth_backoff_s=0.0)
        await lifecycle.tick_once()
        assert lifecycle.status is AuthStatus.AUTH_LOST

        application = create_app(store=store, lifecycle=lifecycle, portfolio_service=portfolio)
        with TestClient(application) as test_client:
            response = test_client.get(
                f"/v1/portfolio/{_ACCOUNT_ID}",
                headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
            )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "30"
        await lifecycle.stop()
    finally:
        await cp_client.aclose()
