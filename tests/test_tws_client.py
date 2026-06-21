"""Tests fuer broker_gateway.tws.client (Karte 441b53db).

Mock-Pattern: ``ib_async.IB`` als ``MagicMock`` mit ``AsyncMock`` fuer
die ``*Async``-Methoden. ``ticker.updateEvent`` wird ueber ein kleines
``FakeEvent`` ersetzt (eventkit-Event-API: ``+= handler``,
``-= handler``, ``fire(arg)``).
"""
from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from ib_async import AccountValue, BarData

from broker_gateway.tws import ClientIdPool, ContractNotFoundError, TWSClient
from broker_gateway.tws.client import (
    _DEFAULT_LIVE_PORT,
    _DEFAULT_PAPER_PORT,
    _SNAPSHOT_TIMEOUT_S,
)


# --------------------------------------------------------------------------
# Fakes & Fixtures
# --------------------------------------------------------------------------

class FakeEvent:
    """Minimaler eventkit-Event-Ersatz fuer Tests."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> FakeEvent:
        self.handlers.append(handler)
        return self

    def __isub__(self, handler: Any) -> FakeEvent:
        if handler in self.handlers:
            self.handlers.remove(handler)
        return self

    def fire(self, arg: Any) -> None:
        for handler in list(self.handlers):
            handler(arg)


def _make_mock_ib() -> MagicMock:
    """Standard-IB-Mock: connect/disconnect/isConnected vorbelegt."""
    ib = MagicMock()
    ib.connectAsync = AsyncMock(return_value=None)
    ib.isConnected = MagicMock(return_value=True)
    ib.disconnect = MagicMock(return_value=None)
    ib.reqMarketDataType = MagicMock(return_value=None)
    ib.cancelMktData = MagicMock(return_value=None)
    return ib


@pytest.fixture
def mock_ib() -> MagicMock:
    return _make_mock_ib()


@pytest.fixture
def pool() -> ClientIdPool:
    return ClientIdPool()


@pytest.fixture
async def connected_client(mock_ib: MagicMock, pool: ClientIdPool) -> Any:
    client = TWSClient(ib=mock_ib, client_id_pool=pool, paper=True)
    await client.connect()
    yield client
    await client.disconnect()


def _portfolio_item(
    *,
    account: str = "U25235077",
    symbol: str = "AAPL",
    con_id: int = 265598,
    position: float = 100.0,
    market_value: float = 15500.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        account=account,
        contract=SimpleNamespace(
            conId=con_id,
            symbol=symbol,
            secType="STK",
            exchange="SMART",
            currency="USD",
        ),
        position=position,
        averageCost=150.0,
        marketPrice=155.0,
        marketValue=market_value,
        unrealizedPNL=500.0,
        realizedPNL=0.0,
    )


def _bar_data(ts: datetime = datetime(2026, 5, 9, 14, 30)) -> BarData:
    bd = BarData()
    bd.date = ts
    bd.open = 100.0
    bd.high = 101.0
    bd.low = 99.0
    bd.close = 100.5
    bd.volume = 1234.0
    bd.average = 100.25
    bd.barCount = 5
    return bd


def _ticker(*, with_event: bool = True) -> MagicMock:
    ticker = MagicMock()
    ticker.contract = SimpleNamespace(conId=265598, symbol="AAPL")
    ticker.last = 150.5
    ticker.lastSize = 50.0
    ticker.bid = 150.4
    ticker.bidSize = 100.0
    ticker.ask = 150.6
    ticker.askSize = 200.0
    ticker.volume = float("nan")
    ticker.marketDataType = 3
    ticker.time = datetime(2026, 5, 9, 14, 30)
    if with_event:
        ticker.updateEvent = FakeEvent()
    return ticker


# --------------------------------------------------------------------------
# ClientIdPool
# --------------------------------------------------------------------------

class TestClientIdPool:
    def test_default_range(self) -> None:
        pool = ClientIdPool()
        assert pool.range == (100, 199)
        assert pool.available() == 100  # 100..199 inkl.

    def test_custom_range(self) -> None:
        pool = ClientIdPool((50, 60))
        assert pool.available() == 11

    def test_invalid_range_start_zero(self) -> None:
        with pytest.raises(ValueError, match="Invalid clientId range"):
            ClientIdPool((0, 50))

    def test_invalid_range_end_below_start(self) -> None:
        with pytest.raises(ValueError, match="Invalid clientId range"):
            ClientIdPool((100, 99))

    async def test_acquire_returns_first_id(self) -> None:
        pool = ClientIdPool((100, 102))
        first = await pool.acquire()
        assert first == 100
        assert pool.available() == 2

    async def test_release_puts_id_back(self) -> None:
        pool = ClientIdPool((100, 102))
        first = await pool.acquire()
        pool.release(first)
        assert pool.available() == 3

    def test_release_out_of_range_below(self) -> None:
        pool = ClientIdPool((100, 102))
        with pytest.raises(ValueError, match="ausserhalb des Pool-Bereichs"):
            pool.release(99)

    def test_release_out_of_range_above(self) -> None:
        pool = ClientIdPool((100, 102))
        with pytest.raises(ValueError, match="ausserhalb des Pool-Bereichs"):
            pool.release(103)

    async def test_two_clients_get_different_ids(self) -> None:
        """Verification-Punkt: zwei TWSClient-Instanzen bekommen
        unterschiedliche clientIds aus demselben Pool."""
        pool = ClientIdPool()
        c1 = TWSClient(ib=_make_mock_ib(), client_id_pool=pool, paper=True)
        c2 = TWSClient(ib=_make_mock_ib(), client_id_pool=pool, paper=False)
        await c1.connect()
        await c2.connect()
        assert c1.client_id == 100
        assert c2.client_id == 101
        assert c1.client_id != c2.client_id
        await c1.disconnect()
        await c2.disconnect()
        # Beide IDs gehen wieder in den Pool zurueck
        assert pool.available() == 100


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

class TestLifecycle:
    async def test_connect_acquires_and_calls_ib(
        self, mock_ib: MagicMock, pool: ClientIdPool
    ) -> None:
        client = TWSClient(ib=mock_ib, client_id_pool=pool, paper=True)
        assert client.client_id is None
        await client.connect()
        assert client.client_id == 100
        mock_ib.connectAsync.assert_awaited_once()
        kwargs = mock_ib.connectAsync.await_args.kwargs
        assert kwargs["clientId"] == 100
        assert kwargs["readonly"] is True

    async def test_connect_idempotent(
        self, mock_ib: MagicMock, pool: ClientIdPool
    ) -> None:
        client = TWSClient(ib=mock_ib, client_id_pool=pool)
        await client.connect()
        await client.connect()
        # Idempotenz gilt nur, solange der Socket steht (isConnected True).
        assert mock_ib.connectAsync.await_count == 1

    async def test_connect_reconnects_after_socket_drop(
        self, mock_ib: MagicMock, pool: ClientIdPool
    ) -> None:
        """Karte 6dbf3026: nach hartem TWS-Socket-Abriss (isConnected
        liefert False, _client_id ist aber noch gesetzt, weil kein
        disconnect() lief) baut connect() die Verbindung autonom neu auf,
        statt wegen des _client_id-Checks als No-op zurueckzukehren."""
        client = TWSClient(ib=mock_ib, client_id_pool=pool)
        await client.connect()
        assert mock_ib.connectAsync.await_count == 1
        assert client.client_id is not None
        # TWS-Prozess-Neustart: Socket weg, _client_id bleibt gesetzt.
        mock_ib.isConnected.return_value = False
        await client.connect()
        # Echter Reconnect statt No-op: connectAsync wird erneut gerufen,
        # der Zombie-State wird vorher sauber aufgeraeumt (disconnect).
        assert mock_ib.connectAsync.await_count == 2
        mock_ib.disconnect.assert_called()
        assert client.client_id is not None

    async def test_connect_releases_stale_id_on_reconnect(
        self, mock_ib: MagicMock
    ) -> None:
        """Beim Reconnect nach Socket-Abriss wird die alte clientId in den
        Pool zurueckgegeben (kein ID-Leak ueber wiederholte Reconnects)."""
        pool = ClientIdPool((100, 101))
        client = TWSClient(ib=mock_ib, client_id_pool=pool)
        await client.connect()
        assert pool.available() == 1  # eine ID gezogen
        mock_ib.isConnected.return_value = False
        await client.connect()
        # Genau eine ID belegt - die alte ist zurueck, eine neue gezogen.
        assert pool.available() == 1

    async def test_disconnect_releases_id(
        self, mock_ib: MagicMock, pool: ClientIdPool
    ) -> None:
        client = TWSClient(ib=mock_ib, client_id_pool=pool)
        await client.connect()
        assert pool.available() == 99
        await client.disconnect()
        assert client.client_id is None
        assert pool.available() == 100

    async def test_connect_failure_returns_id_to_pool(
        self, mock_ib: MagicMock, pool: ClientIdPool
    ) -> None:
        mock_ib.connectAsync.side_effect = ConnectionRefusedError("nope")
        client = TWSClient(ib=mock_ib, client_id_pool=pool)
        with pytest.raises(ConnectionRefusedError):
            await client.connect()
        assert client.client_id is None
        assert pool.available() == 100  # ID zurueck im Pool

    async def test_async_context_manager(
        self, mock_ib: MagicMock, pool: ClientIdPool
    ) -> None:
        async with TWSClient(ib=mock_ib, client_id_pool=pool) as client:
            assert client.client_id == 100
        assert client.client_id is None
        mock_ib.disconnect.assert_called()

    def test_paper_default_port(self, mock_ib: MagicMock) -> None:
        client = TWSClient(ib=mock_ib, client_id_pool=ClientIdPool(), paper=True)
        assert client.port == _DEFAULT_PAPER_PORT

    def test_live_default_port(self, mock_ib: MagicMock) -> None:
        client = TWSClient(ib=mock_ib, client_id_pool=ClientIdPool(), paper=False)
        assert client.port == _DEFAULT_LIVE_PORT

    def test_explicit_port_overrides_default(self, mock_ib: MagicMock) -> None:
        client = TWSClient(
            ib=mock_ib, client_id_pool=ClientIdPool(), port=4099, paper=True
        )
        assert client.port == 4099

    def test_env_var_overrides_default(
        self, mock_ib: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BG_TWS_HOST", "tws-server")
        monkeypatch.setenv("BG_TWS_PORT", "4500")
        client = TWSClient(ib=mock_ib, client_id_pool=ClientIdPool())
        assert client.host == "tws-server"
        assert client.port == 4500

    def test_is_connected_proxies_to_ib(self, mock_ib: MagicMock) -> None:
        mock_ib.isConnected.return_value = False
        client = TWSClient(ib=mock_ib, client_id_pool=ClientIdPool())
        assert client.is_connected() is False
        mock_ib.isConnected.return_value = True
        assert client.is_connected() is True


# --------------------------------------------------------------------------
# account_summary
# --------------------------------------------------------------------------

class TestAccountSummary:
    async def test_returns_dict_keyed_by_tag(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        mock_ib.accountSummaryAsync = AsyncMock(
            return_value=[
                AccountValue("U25235077", "NetLiquidation", "12345.67", "USD", ""),
                AccountValue("U25235077", "BuyingPower", "50000.00", "USD", ""),
            ]
        )
        result = await connected_client.account_summary()
        assert set(result.keys()) == {"NetLiquidation", "BuyingPower"}
        assert result["NetLiquidation"].value == "12345.67"

    async def test_without_account_filter_calls_with_empty_string(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        mock_ib.accountSummaryAsync = AsyncMock(return_value=[])
        await connected_client.account_summary()
        mock_ib.accountSummaryAsync.assert_awaited_with("")

    async def test_with_account_filter_passes_through(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        mock_ib.accountSummaryAsync = AsyncMock(return_value=[])
        await connected_client.account_summary("DUP799747")
        mock_ib.accountSummaryAsync.assert_awaited_with("DUP799747")


# --------------------------------------------------------------------------
# positions
# --------------------------------------------------------------------------

class TestPositions:
    async def test_subscribes_account_updates(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        mock_ib.reqAccountUpdatesAsync = AsyncMock(return_value=None)
        mock_ib.portfolio = MagicMock(return_value=[])
        await connected_client.positions()
        mock_ib.reqAccountUpdatesAsync.assert_awaited_with("")

    async def test_returns_mapped_positions(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        mock_ib.reqAccountUpdatesAsync = AsyncMock(return_value=None)
        mock_ib.portfolio = MagicMock(
            return_value=[_portfolio_item(symbol="AAPL"), _portfolio_item(symbol="MSFT")]
        )
        result = await connected_client.positions()
        assert len(result) == 2
        assert {p.symbol for p in result} == {"AAPL", "MSFT"}
        assert result[0].position == Decimal("100.0")
        assert result[0].market_value == Decimal("15500.0")

    async def test_filters_by_account(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        mock_ib.reqAccountUpdatesAsync = AsyncMock(return_value=None)
        mock_ib.portfolio = MagicMock(
            return_value=[
                _portfolio_item(account="U25235077", symbol="AAPL"),
                _portfolio_item(account="DUP799747", symbol="MSFT"),
            ]
        )
        result = await connected_client.positions(account="DUP799747")
        assert len(result) == 1
        assert result[0].account == "DUP799747"
        assert result[0].symbol == "MSFT"


# --------------------------------------------------------------------------
# qualify
# --------------------------------------------------------------------------

class TestQualify:
    async def test_qualify_returns_first_qualified_contract(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        contract = Stock("AAPL")
        qualified = Stock("AAPL")
        qualified.conId = 265598
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[qualified])
        result = await connected_client.qualify(contract)
        assert result.conId == 265598

    async def test_qualify_raises_when_empty(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[])
        with pytest.raises(ContractNotFoundError):
            await connected_client.qualify(Stock("XYZINVALID"))

    async def test_qualify_raises_when_first_is_none(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[None])
        with pytest.raises(ContractNotFoundError):
            await connected_client.qualify(Stock("XYZINVALID"))


# --------------------------------------------------------------------------
# historical_bars
# --------------------------------------------------------------------------

class TestHistoricalBars:
    async def test_default_arguments_passed_through(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        mock_ib.reqHistoricalDataAsync = AsyncMock(return_value=[_bar_data()])
        await connected_client.historical_bars(Stock("AAPL"))
        kwargs = mock_ib.reqHistoricalDataAsync.await_args.kwargs
        assert kwargs["durationStr"] == "1 D"
        assert kwargs["barSizeSetting"] == "1 hour"
        assert kwargs["whatToShow"] == "TRADES"
        assert kwargs["useRTH"] is True

    async def test_custom_arguments_overridden(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        mock_ib.reqHistoricalDataAsync = AsyncMock(return_value=[])
        await connected_client.historical_bars(
            Stock("AAPL"),
            duration="2 W",
            bar_size="1 day",
            what_to_show="MIDPOINT",
            use_rth=False,
        )
        kwargs = mock_ib.reqHistoricalDataAsync.await_args.kwargs
        assert kwargs["durationStr"] == "2 W"
        assert kwargs["barSizeSetting"] == "1 day"
        assert kwargs["whatToShow"] == "MIDPOINT"
        assert kwargs["useRTH"] is False

    async def test_bars_are_mapped_to_pydantic_with_utc(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        mock_ib.reqHistoricalDataAsync = AsyncMock(return_value=[_bar_data()])
        bars = await connected_client.historical_bars(Stock("AAPL"))
        assert len(bars) == 1
        assert bars[0].timestamp.tzinfo == UTC
        assert bars[0].close == Decimal("100.5")


# --------------------------------------------------------------------------
# market_snapshot
# --------------------------------------------------------------------------

class TestMarketSnapshot:
    async def test_default_market_data_type_is_delayed(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        ticker = _ticker()
        mock_ib.reqMktData = MagicMock(return_value=ticker)
        mock_ib.reqMarketDataType = MagicMock(return_value=None)

        async def trigger() -> None:
            await asyncio.sleep(0.01)
            ticker.updateEvent.fire(ticker)

        asyncio.create_task(trigger())
        await connected_client.market_snapshot(Stock("AAPL"))
        mock_ib.reqMarketDataType.assert_called_with(3)

    async def test_custom_market_data_type_passed(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        ticker = _ticker()
        mock_ib.reqMktData = MagicMock(return_value=ticker)

        async def trigger() -> None:
            await asyncio.sleep(0.01)
            ticker.updateEvent.fire(ticker)

        asyncio.create_task(trigger())
        await connected_client.market_snapshot(Stock("AAPL"), market_data_type=1)
        mock_ib.reqMarketDataType.assert_called_with(1)

    async def test_returns_snapshot_after_first_update(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        ticker = _ticker()
        mock_ib.reqMktData = MagicMock(return_value=ticker)

        async def trigger() -> None:
            await asyncio.sleep(0.01)
            ticker.updateEvent.fire(ticker)

        asyncio.create_task(trigger())
        snap = await connected_client.market_snapshot(Stock("AAPL"))
        assert snap.last == Decimal("150.5")
        assert snap.bid == Decimal("150.4")

    async def test_returns_partial_snapshot_on_timeout(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        ticker = _ticker()
        # alle Werte nan (kein update kommt)
        ticker.last = float("nan")
        ticker.bid = float("nan")
        mock_ib.reqMktData = MagicMock(return_value=ticker)
        # kein trigger - timeout greift
        snap = await connected_client.market_snapshot(
            Stock("AAPL"), timeout=0.05
        )
        # Methode raised nicht, sondern liefert leeren Snapshot
        assert snap.last is None
        assert snap.bid is None

    async def test_subscription_conflict_propagates(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        """IBKR Error 10197 (Marketdata-Subscription-Konflikt) wirft die
        ib_async-Library als Exception. Der Adapter reicht das durch."""
        from ib_async import Stock

        def _raise(*args: object, **kwargs: object) -> None:
            raise RuntimeError("Error 10197: Market data subscription required")

        mock_ib.reqMktData = MagicMock(side_effect=_raise)
        with pytest.raises(RuntimeError, match="10197"):
            await connected_client.market_snapshot(Stock("AAPL"))


# --------------------------------------------------------------------------
# market_stream
# --------------------------------------------------------------------------

class TestMarketStream:
    async def test_yields_ticks_on_update_events(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        ticker = _ticker()
        mock_ib.reqMktData = MagicMock(return_value=ticker)

        stream = connected_client.market_stream(
            Stock("AAPL"), poll_interval=0.05
        )

        async def fire_then_disconnect() -> None:
            await asyncio.sleep(0.01)
            ticker.updateEvent.fire(ticker)
            await asyncio.sleep(0.01)
            ticker.updateEvent.fire(ticker)
            await asyncio.sleep(0.01)
            mock_ib.isConnected.return_value = False

        task = asyncio.create_task(fire_then_disconnect())
        ticks = [tick async for tick in stream]
        await task
        assert len(ticks) == 2
        assert ticks[0].field == "last"
        assert ticks[0].value == Decimal("150.5")

    async def test_field_bid_passed_to_tick_mapping(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        ticker = _ticker()
        mock_ib.reqMktData = MagicMock(return_value=ticker)
        stream = connected_client.market_stream(
            Stock("AAPL"), field="bid", poll_interval=0.05
        )

        async def fire_then_disconnect() -> None:
            await asyncio.sleep(0.01)
            ticker.updateEvent.fire(ticker)
            await asyncio.sleep(0.01)
            mock_ib.isConnected.return_value = False

        task = asyncio.create_task(fire_then_disconnect())
        ticks = [tick async for tick in stream]
        await task
        assert ticks[0].field == "bid"
        assert ticks[0].value == Decimal("150.4")

    async def test_cancel_called_on_cleanup(
        self, connected_client: TWSClient, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        contract = Stock("AAPL")
        ticker = _ticker()
        mock_ib.reqMktData = MagicMock(return_value=ticker)

        async def disconnect_quickly() -> None:
            await asyncio.sleep(0.01)
            mock_ib.isConnected.return_value = False

        task = asyncio.create_task(disconnect_quickly())
        stream = connected_client.market_stream(contract, poll_interval=0.05)
        async for _ in stream:
            pass
        await task
        mock_ib.cancelMktData.assert_called_with(contract)


# --------------------------------------------------------------------------
# Disziplin: alle Read-Methoden sind async (Coroutines)
# --------------------------------------------------------------------------

class TestAsyncDiscipline:
    """Verification-Punkt: TWSClient-Methoden sind async und liefern bei
    sync-Aufruf eine Coroutine zurueck (kein implizit-sync-Wrapping)."""

    @pytest.mark.parametrize(
        "method_name",
        ["account_summary", "positions", "qualify", "historical_bars", "market_snapshot"],
    )
    def test_method_returns_coroutine_when_called_sync(
        self, mock_ib: MagicMock, method_name: str
    ) -> None:
        client = TWSClient(ib=mock_ib, client_id_pool=ClientIdPool())
        method = getattr(client, method_name)
        # Mit dummy-Args aufrufen, dass kein echter Call passiert
        if method_name in {"qualify", "historical_bars", "market_snapshot"}:
            from ib_async import Stock

            coro = method(Stock("AAPL"))
        else:
            coro = method()
        try:
            assert inspect.iscoroutine(coro), (
                f"{method_name} muss eine Coroutine zurueckgeben, kein Sync-Wrapper"
            )
        finally:
            coro.close()

    async def test_market_stream_returns_async_generator(
        self, mock_ib: MagicMock
    ) -> None:
        from ib_async import Stock

        client = TWSClient(ib=mock_ib, client_id_pool=ClientIdPool())
        gen = client.market_stream(Stock("AAPL"))
        try:
            assert inspect.isasyncgen(gen), (
                "market_stream muss ein AsyncGenerator sein"
            )
        finally:
            await gen.aclose()

    async def test_asyncio_run_in_running_loop_raises(
        self, mock_ib: MagicMock
    ) -> None:
        """Zweite Disziplin-Variante: wer eine async-Methode synchron via
        ``asyncio.run(...)`` aus einem schon laufenden Loop ruft, bekommt
        einen RuntimeError. Das ist der natuerliche Sicherheits-Mechanismus
        von Python gegen ueberlappende Loops und genau das Verhalten, das
        die Karte mit "ueberlappenden Loop-Hinweis" meint."""
        client = TWSClient(ib=mock_ib, client_id_pool=ClientIdPool())
        coro = client.account_summary()
        try:
            with pytest.raises(RuntimeError, match="cannot be called from a running"):
                asyncio.run(coro)
        finally:
            coro.close()


# --------------------------------------------------------------------------
# Sanity-Check der Defaults
# --------------------------------------------------------------------------

def test_snapshot_timeout_default_is_ten_seconds() -> None:
    assert _SNAPSHOT_TIMEOUT_S == 10.0
