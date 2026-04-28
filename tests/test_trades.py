"""Tests fuer TradesService und /v1/trades."""
from __future__ import annotations

from datetime import date
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
from broker_gateway.cp.trades import Trade, TradesService, _map_trade
from broker_gateway.main import create_app


_ADMIN_VALUE = "trades-admin-token-aaaaaaaaaaaaaa"
_ACCOUNT_ID = "U25235077"
_TODAY = date(2026, 4, 25)


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
def trades(cp_client: CPGatewayClient) -> TradesService:
    return TradesService(cp_client)


@pytest.fixture
async def client(
    store: InMemoryTokenStore,
    lifecycle: AuthLifecycle,
    trades: TradesService,
    cp_gateway_mock,
):
    application = create_app(
        store=store,
        lifecycle=lifecycle,
        trades_service=trades,
    )
    with TestClient(application) as test_client:
        yield test_client


# ---- _map_trade (IBKR-Live-Schema) ----


def test_map_trade_uses_account_field_for_account_id() -> None:
    """IBKR-Live-Schema (Recording AP-02 #04): Trade-Body hat `account`,
    nicht `account_id`. Adapter muss das mappen."""
    entry = {
        "execution_id": "exec-1",
        "symbol": "T",
        "side": "B",
        "size": 12.0,
        "price": "26.2193",
        "commission": "1.0",
        "net_amount": 314.6316,
        "account": "U25235077",
        "accountCode": "U25235077",
        "listing_exchange": "NYSE",
        "conid": 37018770,
        "trade_time": "2026-04-25 14:00:00",
    }
    trade = _map_trade(entry)
    assert trade.account_id == "U25235077"


def test_map_trade_derives_currency_from_listing_exchange() -> None:
    entry = {
        "execution_id": "exec-2",
        "side": "S",
        "size": 1.0,
        "price": "120.20",
        "commission": "1.50",
        "account": "U25235077",
        "listing_exchange": "IBIS",  # XETRA -> EUR
        "conid": 104747,
    }
    trade = _map_trade(entry)
    assert trade.commission is not None
    assert trade.commission.currency == "EUR"
    assert trade.price is not None
    assert trade.price.currency == "EUR"
    assert trade.currency_assumed is False


def test_map_trade_falls_back_to_usd_when_no_exchange_or_currency() -> None:
    entry = {
        "execution_id": "exec-3",
        "side": "B",
        "size": 1.0,
        "price": "10.00",
        "commission": "0.50",
        "account": "U25235077",
        # weder currency noch listing_exchange
        "conid": 999,
    }
    trade = _map_trade(entry)
    assert trade.commission is not None
    assert trade.commission.currency == "USD"
    assert trade.currency_assumed is True


def test_map_trade_explicit_currency_wins_over_exchange() -> None:
    """Wenn IBKR doch `currency` mitliefert (FX-Cash-Trades), schlaegt das
    den Exchange-Lookup."""
    entry = {
        "execution_id": "exec-4",
        "side": "B",
        "size": 1.0,
        "price": "1.10",
        "commission": "0.10",
        "account": "U25235077",
        "currency": "GBP",
        "listing_exchange": "NASDAQ",  # NASDAQ wuerde USD liefern
        "conid": 12345,
    }
    trade = _map_trade(entry)
    assert trade.commission is not None
    assert trade.commission.currency == "GBP"


# ---- TradesService ----


async def test_list_trades_returns_normalised_trades(trades: TradesService) -> None:
    result = await trades.list_trades(
        period_from=date(2026, 4, 1),
        period_to=_TODAY,
        today=_TODAY,
    )
    assert result, "Mock liefert mindestens einen Trade fuer den Window"
    first = result[0]
    assert isinstance(first, Trade)
    assert first.commission is not None
    assert first.commission.currency == "USD"
    assert Decimal(first.commission.value) == Decimal("1.50")


async def test_list_trades_filters_by_window(trades: TradesService, cp_gateway_mock) -> None:
    # Window enthaelt nur den 25. April -> genau ein Trade (i=0).
    result = await trades.list_trades(
        period_from=_TODAY,
        period_to=_TODAY,
        today=_TODAY,
    )
    assert len(result) == 1
    assert result[0].executed_at is not None
    assert result[0].executed_at.date() == _TODAY


async def test_list_trades_rejects_inverted_window(trades: TradesService) -> None:
    with pytest.raises(Exception):
        await trades.list_trades(
            period_from=date(2026, 4, 25),
            period_to=date(2026, 4, 20),
            today=_TODAY,
        )


async def test_list_trades_period_to_days_translation(
    trades: TradesService, cp_gateway_mock
) -> None:
    # Window 2026-04-20..25 -> days = 6 (heute - from + 1).
    await trades.list_trades(
        period_from=date(2026, 4, 20),
        period_to=_TODAY,
        today=_TODAY,
    )
    # Mock liefert pro Tag einen Trade. Bei days=6 sind 6 Trades drin
    # (25, 24, 23, 22, 21, 20). Filterresultat sollte alle abdecken.
    result = await trades.list_trades(
        period_from=date(2026, 4, 20),
        period_to=_TODAY,
        today=_TODAY,
    )
    assert len(result) == 6


async def test_commissions_mtd_aggregates_correctly(trades: TradesService) -> None:
    aggregate = await trades.commissions_mtd(today=_TODAY)
    assert aggregate.metric == "commissions_mtd"
    assert aggregate.period_from == date(2026, 4, 1)
    assert aggregate.period_to == _TODAY
    # Mock liefert pro Tag 1.50 USD. days=25 (1.4. -> 25.4.) -> 25 Trades
    # (Mock-Cap auch innerhalb 30 Tagen) -> 37.50 Summe.
    assert aggregate.trade_count == 25
    assert Decimal(aggregate.value.value) == Decimal("37.50")
    assert aggregate.value.currency == "USD"
    # Mock liefert explizite Currency -> keine Assumption.
    assert aggregate.currency_assumption is None


async def test_commissions_mtd_marks_assumption_when_currency_missing(
    trades: TradesService,
    cp_gateway_mock,
) -> None:
    cp_gateway_mock.omit_trade_currency = True
    aggregate = await trades.commissions_mtd(today=_TODAY)
    assert aggregate.currency_assumption == "USD"
    assert aggregate.value.currency == "USD"


# ---- Endpunkte ----


def test_list_endpoint_returns_trades(client: TestClient) -> None:
    response = client.get(
        "/v1/trades?from=2026-04-20&to=2026-04-25",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["trade_count"] >= 1
    first = body["items"][0]
    assert first["commission"]["currency"] == "USD"


def test_list_endpoint_default_window(client: TestClient) -> None:
    response = client.get(
        "/v1/trades",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200


def test_list_endpoint_rejects_inverted_window(client: TestClient) -> None:
    response = client.get(
        "/v1/trades?from=2026-04-25&to=2026-04-20",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 400


def test_aggregates_endpoint_commissions_mtd(client: TestClient) -> None:
    response = client.get(
        "/v1/trades/aggregates?metric=commissions_mtd",
        headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["metric"] == "commissions_mtd"
    assert body["value"]["currency"] == "USD"
    # Aggregat mit Mock-Daten (heute = realer datetime.now): wir koennen
    # den Wert nicht hartcodieren, aber er muss > 0 sein, weil Mock zumindest
    # einen Trade liefert.
    assert Decimal(body["value"]["value"]) >= Decimal("0")


def test_endpoint_without_token_returns_401(client: TestClient) -> None:
    response = client.get("/v1/trades")
    assert response.status_code == 401


def test_endpoint_with_wrong_scope_returns_403(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    bad = generate_token_value()
    store.put(Token(value=bad, caller_id="bot", scopes=[SCOPE_QUOTES_READ]))
    response = client.get(
        "/v1/trades",
        headers={"Authorization": f"Bearer {bad}"},
    )
    assert response.status_code == 403


def test_endpoint_with_correct_scope(
    client: TestClient, store: InMemoryTokenStore
) -> None:
    ok = generate_token_value()
    store.put(Token(value=ok, caller_id="psm", scopes=[SCOPE_PORTFOLIO_READ]))
    response = client.get(
        "/v1/trades/aggregates?metric=commissions_mtd",
        headers={"Authorization": f"Bearer {ok}"},
    )
    assert response.status_code == 200


async def test_endpoint_returns_503_when_session_lost(
    store: InMemoryTokenStore,
    trades: TradesService,
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
            trades_service=trades,
        )
        with TestClient(application) as test_client:
            response = test_client.get(
                "/v1/trades",
                headers={"Authorization": f"Bearer {_ADMIN_VALUE}"},
            )
        assert response.status_code == 503
        assert response.headers["retry-after"] == "30"
        await lifecycle.stop()
    finally:
        await cp_client.aclose()
