"""Tests fuer broker_gateway.tws.portfolio (Karte 23a368ee, Phase 1;
Subscribe-Cache-Fix in Karte 4c5b226d, v2.1.1).

Coverage-Ziel: >=90% fuer src/broker_gateway/tws/portfolio.py.

Mock-Strategie: TWSClient.``_ib`` wird durch einen SimpleNamespace
ersetzt, der die ib_async-Methoden ``reqAccountUpdates`` (sync),
``portfolio()``, ``accountValues(account)`` simuliert. Die Test-Daten
sind an die 18 Symbole des Live-Accounts U25235077 angelehnt (siehe
Memory ``project_post_cutover_http_api_holdout``).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from broker_gateway.tws.portfolio import (
    Ledger,
    PortfolioSummary,
    Position,
    TWSPortfolioService,
)


# --------------------------------------------------------------------------
# Fixture-Helper
# --------------------------------------------------------------------------


def _make_contract(
    *, conid: int, symbol: str, currency: str = "USD", exchange: str = "NASDAQ"
) -> SimpleNamespace:
    return SimpleNamespace(
        conId=conid,
        symbol=symbol,
        currency=currency,
        exchange=exchange,
        secType="STK",
    )


def _make_portfolio_item(
    *,
    account: str,
    conid: int,
    symbol: str,
    position: float,
    average_cost: float,
    market_price: float | None = None,
    market_value: float | None = None,
    currency: str = "USD",
) -> SimpleNamespace:
    return SimpleNamespace(
        account=account,
        contract=_make_contract(conid=conid, symbol=symbol, currency=currency),
        position=position,
        averageCost=average_cost,
        marketPrice=market_price,
        marketValue=market_value,
        unrealizedPNL=None,
        realizedPNL=None,
    )


def _make_account_value(
    *, tag: str, value: str, currency: str | None = "USD", account: str = ""
) -> SimpleNamespace:
    return SimpleNamespace(
        tag=tag,
        value=value,
        currency=currency,
        account=account,
        modelCode=None,
    )


def _make_client(
    *,
    portfolio_items: list[SimpleNamespace] | None = None,
    account_values: list[SimpleNamespace] | None = None,
    account_values_filter_supported: bool = True,
) -> MagicMock:
    """Baut einen TWSClient-Mock mit konfigurierbaren ib_async-Antworten.

    ``account_values_filter_supported``: kontrolliert, ob ``accountValues``
    das ``account``-Argument akzeptiert. False → TypeError, der den
    Fallback in ``_fetch_account_values`` triggert.
    """
    items = portfolio_items if portfolio_items is not None else []
    values = account_values if account_values is not None else []

    def _account_values(account: str = "") -> list[SimpleNamespace]:
        if not account_values_filter_supported:
            raise TypeError("accountValues() takes no arguments in this mock")
        if not account:
            return list(values)
        return [
            v for v in values
            if not getattr(v, "account", "")
            or getattr(v, "account", "") == account
        ]

    def _account_values_no_args(account: Any = None) -> list[SimpleNamespace]:
        if account is not None:
            raise TypeError("accountValues() takes no arguments in this mock")
        return list(values)

    ib = SimpleNamespace(
        reqAccountUpdates=MagicMock(return_value=None),
        portfolio=MagicMock(return_value=list(items)),
        accountValues=MagicMock(
            side_effect=_account_values
            if account_values_filter_supported
            else _account_values_no_args
        ),
    )
    client = MagicMock()
    client._ib = ib
    return client


# --------------------------------------------------------------------------
# positions(...)
# --------------------------------------------------------------------------


class TestPositions:
    async def test_positions_filters_by_account(self) -> None:
        items = [
            _make_portfolio_item(
                account="U25235077",
                conid=265598,
                symbol="AAPL",
                position=10.0,
                average_cost=150.5,
                market_price=180.0,
                market_value=1800.0,
            ),
            _make_portfolio_item(
                account="DUP799747",
                conid=42,
                symbol="OTHER",
                position=99.0,
                average_cost=1.0,
            ),
        ]
        client = _make_client(portfolio_items=items)
        service = TWSPortfolioService(client)
        result = await service.positions("U25235077")
        assert len(result) == 1
        pos = result[0]
        assert isinstance(pos, Position)
        assert pos.account_id == "U25235077"
        assert pos.conid == 265598
        # Decimal(str(10.0)) -> "10.0"; semantisch identisch zur cp-Variante.
        assert Decimal(pos.quantity) == Decimal("10")
        assert pos.avg_cost is not None
        assert Decimal(pos.avg_cost.value) == Decimal("150.5")
        assert pos.avg_cost.currency == "USD"
        assert pos.market_price is not None
        assert Decimal(pos.market_price.value) == Decimal("180")
        assert pos.market_value is not None
        assert Decimal(pos.market_value.value) == Decimal("1800")

    async def test_positions_empty_when_no_items(self) -> None:
        client = _make_client(portfolio_items=[])
        service = TWSPortfolioService(client)
        assert await service.positions("U25235077") == []

    async def test_positions_handles_fractional_quantity(self) -> None:
        items = [
            _make_portfolio_item(
                account="U25235077",
                conid=11,
                symbol="NRT",
                position=153.7282,
                average_cost=8.4102949,
            ),
        ]
        client = _make_client(portfolio_items=items)
        service = TWSPortfolioService(client)
        result = await service.positions("U25235077")
        assert len(result) == 1
        # Decimal-String-Roundtrip darf keine Praezision verlieren
        assert Decimal(result[0].quantity) == Decimal("153.7282")

    async def test_positions_calls_req_account_updates(self) -> None:
        client = _make_client(portfolio_items=[])
        service = TWSPortfolioService(client)
        await service.positions("U25235077")
        # Sync-Subscribe-Frame, nicht await: reqAccountUpdatesAsync haengt
        # bei Re-Subscribes (Memory project_tws_portfolio_resubscribe_hang).
        client._ib.reqAccountUpdates.assert_called_once_with(
            True, "U25235077"
        )

    async def test_positions_without_filter_returns_all(self) -> None:
        items = [
            _make_portfolio_item(
                account="A",
                conid=1,
                symbol="X",
                position=1.0,
                average_cost=1.0,
            ),
            _make_portfolio_item(
                account="B",
                conid=2,
                symbol="Y",
                position=2.0,
                average_cost=2.0,
            ),
        ]
        client = _make_client(portfolio_items=items)
        service = TWSPortfolioService(client)
        result = await service.positions("")
        assert len(result) == 2

    async def test_positions_omits_missing_currency(self) -> None:
        # Wenn IBKR die Currency leer liefert, normalize_money → None.
        item = _make_portfolio_item(
            account="U25235077",
            conid=1,
            symbol="X",
            position=1.0,
            average_cost=1.0,
            currency="",
        )
        client = _make_client(portfolio_items=[item])
        service = TWSPortfolioService(client)
        result = await service.positions("U25235077")
        assert result[0].avg_cost is None


# --------------------------------------------------------------------------
# ledger(...)
# --------------------------------------------------------------------------


class TestLedger:
    async def test_ledger_groups_by_currency(self) -> None:
        values = [
            _make_account_value(
                tag="CashBalance", value="1000.5", currency="USD",
                account="U25235077"
            ),
            _make_account_value(
                tag="SettledCash", value="900.0", currency="USD",
                account="U25235077"
            ),
            _make_account_value(
                tag="CashBalance", value="50", currency="EUR",
                account="U25235077"
            ),
        ]
        client = _make_client(account_values=values)
        service = TWSPortfolioService(client)
        ledger = await service.ledger("U25235077")
        assert isinstance(ledger, Ledger)
        assert ledger.account_id == "U25235077"
        currencies = {e.currency: e for e in ledger.entries}
        assert "USD" in currencies and "EUR" in currencies
        usd = currencies["USD"]
        assert usd.cash_balance is not None and usd.cash_balance.value == "1000.5"
        assert usd.settled_cash is not None and usd.settled_cash.value == "900.0"
        eur = currencies["EUR"]
        assert eur.cash_balance is not None and eur.cash_balance.value == "50"
        assert eur.settled_cash is None

    async def test_ledger_ignores_base_aggregate(self) -> None:
        values = [
            _make_account_value(
                tag="CashBalance", value="1234", currency="BASE"
            ),
            _make_account_value(
                tag="CashBalance", value="100", currency="USD"
            ),
        ]
        client = _make_client(account_values=values)
        service = TWSPortfolioService(client)
        ledger = await service.ledger("U25235077")
        assert all(e.currency != "BASE" for e in ledger.entries)
        assert {e.currency for e in ledger.entries} == {"USD"}

    async def test_ledger_empty_when_no_values(self) -> None:
        client = _make_client(account_values=[])
        service = TWSPortfolioService(client)
        ledger = await service.ledger("U25235077")
        assert ledger.entries == []

    async def test_ledger_falls_back_when_filter_unsupported(self) -> None:
        # Manche Mock-Implementierungen lassen kein account-Arg zu;
        # _fetch_account_values muss dann selbst filtern.
        values = [
            _make_account_value(
                tag="CashBalance", value="1", currency="USD",
                account="U25235077"
            ),
            _make_account_value(
                tag="CashBalance", value="2", currency="USD",
                account="OTHER"
            ),
        ]
        client = _make_client(
            account_values=values,
            account_values_filter_supported=False,
        )
        service = TWSPortfolioService(client)
        ledger = await service.ledger("U25235077")
        # Nur U25235077 bleibt ueber - der Fallback-Filter greift.
        assert len(ledger.entries) == 1
        assert ledger.entries[0].cash_balance is not None
        assert ledger.entries[0].cash_balance.value == "1"


# --------------------------------------------------------------------------
# summary(...)
# --------------------------------------------------------------------------


class TestSummary:
    async def test_summary_aggregates_account_fields(self) -> None:
        values = [
            _make_account_value(
                tag="NetLiquidation", value="50000", currency="USD"
            ),
            _make_account_value(
                tag="TotalCashValue", value="10000", currency="USD"
            ),
            _make_account_value(
                tag="GrossPositionValue", value="40000", currency="USD"
            ),
        ]
        items = [
            _make_portfolio_item(
                account="U25235077",
                conid=1,
                symbol="X",
                position=1.0,
                average_cost=1.0,
            ),
            _make_portfolio_item(
                account="U25235077",
                conid=2,
                symbol="Y",
                position=2.0,
                average_cost=2.0,
            ),
        ]
        client = _make_client(portfolio_items=items, account_values=values)
        service = TWSPortfolioService(client)
        summary = await service.summary("U25235077")
        assert isinstance(summary, PortfolioSummary)
        assert summary.account_id == "U25235077"
        assert summary.base_currency == "USD"
        assert summary.net_liquidation is not None
        assert summary.net_liquidation.value == "50000"
        assert summary.cash_total is not None
        assert summary.cash_total.value == "10000"
        assert summary.positions_value is not None
        assert summary.positions_value.value == "40000"
        assert summary.position_count == 2

    async def test_summary_ignores_base_currency_aggregate(self) -> None:
        values = [
            _make_account_value(
                tag="NetLiquidation", value="9999", currency="BASE"
            ),
            _make_account_value(
                tag="NetLiquidation", value="100", currency="USD"
            ),
        ]
        client = _make_client(portfolio_items=[], account_values=values)
        service = TWSPortfolioService(client)
        summary = await service.summary("U25235077")
        assert summary.net_liquidation is not None
        assert summary.net_liquidation.value == "100"

    async def test_summary_empty_account(self) -> None:
        client = _make_client(portfolio_items=[], account_values=[])
        service = TWSPortfolioService(client)
        summary = await service.summary("U25235077")
        assert summary.position_count == 0
        assert summary.base_currency is None
        assert summary.net_liquidation is None

    async def test_summary_uses_first_non_base_currency(self) -> None:
        # Wenn NetLiquidation fehlt, soll TotalCashValue oder
        # GrossPositionValue die base_currency setzen.
        values = [
            _make_account_value(
                tag="TotalCashValue", value="500", currency="EUR"
            ),
        ]
        client = _make_client(portfolio_items=[], account_values=values)
        service = TWSPortfolioService(client)
        summary = await service.summary("U25235077")
        assert summary.base_currency == "EUR"
        assert summary.cash_total is not None
        assert summary.cash_total.value == "500"


# --------------------------------------------------------------------------
# Schema-Compat (TTL, invalidate)
# --------------------------------------------------------------------------


class TestSchemaCompat:
    def test_ttl_seconds_is_zero(self) -> None:
        client = _make_client()
        service = TWSPortfolioService(client)
        assert service.ttl_seconds == 0.0

    def test_invalidate_is_noop(self) -> None:
        client = _make_client()
        service = TWSPortfolioService(client)
        # Darf nicht raisen, kein Side-Effect
        assert service.invalidate("U25235077") is None


# --------------------------------------------------------------------------
# _ensure_subscribed - Resubscribe-Hang-Bug-Fix (Karte 4c5b226d, v2.1.1)
# --------------------------------------------------------------------------


class TestEnsureSubscribed:
    """Verriegelt den Subscribe-Cache gegen Resubscribe-Hangs.

    Hintergrund: ``ib.reqAccountUpdatesAsync(account_id)`` resolvet nur
    beim allerersten ``accountDownloadEnd``-Trigger pro Account; ein
    zweiter Aufruf haengt indefinit. Der Service muss deshalb pro
    Account genau einmal subscriben - und zwar synchron via
    ``ib.reqAccountUpdates(True, account_id)`` (Fire-and-Forget).
    """

    async def test_subscribe_idempotent_across_calls(self) -> None:
        client = _make_client(portfolio_items=[])
        service = TWSPortfolioService(client)
        await service.positions("U25235077")
        await service.positions("U25235077")
        await service.summary("U25235077")
        client._ib.reqAccountUpdates.assert_called_once_with(
            True, "U25235077"
        )

    async def test_subscribe_lock_serializes_concurrent_first_calls(
        self,
    ) -> None:
        # Zwei parallele Calls duerfen nicht beide den Subscribe-Frame
        # senden - der asyncio.Lock muss greifen.
        client = _make_client(portfolio_items=[])
        service = TWSPortfolioService(client)
        await asyncio.gather(
            service.positions("U25235077"),
            service.positions("U25235077"),
        )
        assert client._ib.reqAccountUpdates.call_count == 1

    async def test_empty_account_id_skips_subscribe(self) -> None:
        # positions("") liest den Wildcard-Cache ohne Subscribe -
        # ib.portfolio() liefert dann den primaer-Account-Cache.
        client = _make_client(portfolio_items=[])
        service = TWSPortfolioService(client)
        await service.positions("")
        client._ib.reqAccountUpdates.assert_not_called()

    async def test_invalidate_does_not_clear_subscribe_cache(self) -> None:
        # invalidate() bleibt no-op auch bei Subscribe-Cache - die
        # ib_async-Daten sind durch updatePortfolio-Events frisch,
        # ein Resubscribe wuerde nur den Hang-Bug riskieren.
        client = _make_client(portfolio_items=[])
        service = TWSPortfolioService(client)
        await service.positions("U25235077")
        service.invalidate("U25235077")
        await service.positions("U25235077")
        client._ib.reqAccountUpdates.assert_called_once_with(
            True, "U25235077"
        )

    async def test_different_accounts_subscribe_independently(self) -> None:
        # Multi-Account-Pfad: jeder Account braucht seinen eigenen
        # Subscribe genau einmal.
        client = _make_client(portfolio_items=[])
        service = TWSPortfolioService(client)
        await service.positions("U25235077")
        await service.positions("DUP799747")
        await service.positions("U25235077")
        await service.positions("DUP799747")
        assert client._ib.reqAccountUpdates.call_count == 2
        calls = {
            args[0]
            for args in client._ib.reqAccountUpdates.call_args_list
        }
        assert calls == {(True, "U25235077"), (True, "DUP799747")}

    async def test_missing_req_account_updates_is_tolerated(self) -> None:
        # Falls eine Mock-/Stub-Implementierung reqAccountUpdates nicht
        # exponiert (z.B. ein minimaler Test-Stub), darf das Service-
        # Modul nicht crashen - der Subscribe-Cache wird trotzdem
        # gefuellt.
        client = _make_client(portfolio_items=[])
        del client._ib.reqAccountUpdates
        service = TWSPortfolioService(client)
        await service.positions("U25235077")
        assert "U25235077" in service._subscribed_accounts
