"""Tests fuer broker_gateway.tws.quotes (Karte fa0f5e6c, Phase 3).

Coverage-Ziel: >=90% fuer src/broker_gateway/tws/quotes.py.

Mock-Strategie: TWSClient.``_ib`` wird durch einen SimpleNamespace
ersetzt, der die ib_async-Methoden ``reqMarketDataType``,
``reqMktDataAsync``, ``reqMktData``, ``qualifyContractsAsync`` und
``cancelMktData`` simuliert. ``pendingTickersEvent`` ist ein einfacher
Listener-Holder mit ``__iadd__``/``__isub__``/``emit``.
"""
from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from broker_gateway.tws.quotes import (
    FIELD_ALIASES,
    Quote,
    TWSQuotesService,
    _availability_for_market_data_type,
    _change_pct_from_ticker,
    _quote_from_ticker,
    _str_or_none,
    _ticker_time_to_datetime,
    _volume_from_ticker,
)


# --------------------------------------------------------------------------
# Fixture-Helper
# --------------------------------------------------------------------------


class _PendingTickersEvent:
    """Minimaler ib_async-Event-Stub mit += / -= / emit()."""

    def __init__(self) -> None:
        self._listeners: list[Any] = []

    def __iadd__(self, listener: Any) -> "_PendingTickersEvent":
        self._listeners.append(listener)
        return self

    def __isub__(self, listener: Any) -> "_PendingTickersEvent":
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass
        return self

    def emit(self, tickers: Any) -> None:
        for listener in list(self._listeners):
            listener(tickers)

    @property
    def listener_count(self) -> int:
        return len(self._listeners)


def _make_contract(*, conid: int, symbol: str = "AAPL") -> SimpleNamespace:
    return SimpleNamespace(
        conId=conid,
        symbol=symbol,
        exchange="SMART",
        currency="USD",
        secType="STK",
    )


def _make_ticker(
    *,
    conid: int,
    last: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
    volume: float | None = None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    time: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        contract=_make_contract(conid=conid),
        last=last,
        bid=bid,
        ask=ask,
        volume=volume,
        high=high,
        low=low,
        close=close,
        time=time,
    )


def _make_client(
    *,
    qualify_returns: list[Any] | None = None,
    qualify_side_effect: Any = None,
    snapshot_returns: list[Any] | None = None,
    snapshot_side_effect: Any = None,
    stream_ticker: Any | None = None,
) -> MagicMock:
    """Baut einen TWSClient-Mock mit konfigurierbaren ib_async-Antworten."""
    qualify_mock = AsyncMock()
    if qualify_side_effect is not None:
        qualify_mock.side_effect = qualify_side_effect
    elif qualify_returns is not None:
        qualify_mock.side_effect = [list(r) if r is not None else [] for r in qualify_returns]
    else:
        qualify_mock.return_value = []

    snapshot_mock = AsyncMock()
    if snapshot_side_effect is not None:
        snapshot_mock.side_effect = snapshot_side_effect
    elif snapshot_returns is not None:
        snapshot_mock.side_effect = list(snapshot_returns)
    else:
        snapshot_mock.return_value = None

    pending_event = _PendingTickersEvent()
    cancel_mock = MagicMock()
    req_mkt_data_mock = MagicMock(return_value=stream_ticker)

    ib = SimpleNamespace(
        reqMarketDataType=MagicMock(),
        reqMktDataAsync=snapshot_mock,
        reqMktData=req_mkt_data_mock,
        qualifyContractsAsync=qualify_mock,
        cancelMktData=cancel_mock,
        pendingTickersEvent=pending_event,
    )
    client = MagicMock()
    client._ib = ib
    return client


# --------------------------------------------------------------------------
# snapshot_with_prime(...)
# --------------------------------------------------------------------------


class TestSnapshotWithPrime:
    async def test_snapshot_returns_quotes(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(
            conid=265598, last=180.5, bid=180.4, ask=180.6, volume=129000000.0
        )
        client = _make_client(
            qualify_returns=[[contract]],
            snapshot_returns=[ticker],
        )
        service = TWSQuotesService(client)
        result = await service.snapshot_with_prime([265598], list(FIELD_ALIASES.values()))
        assert len(result) == 1
        quote = result[0]
        assert isinstance(quote, Quote)
        assert quote.conid == 265598
        assert Decimal(quote.last) == Decimal("180.5")
        assert Decimal(quote.bid) == Decimal("180.4")
        assert Decimal(quote.ask) == Decimal("180.6")
        assert quote.volume == "129000000"
        # Default market_data_type=3 -> DPB / delayed
        assert quote.availability_raw == "DPB"
        assert quote.availability == "delayed"

    async def test_snapshot_empty_conids(self) -> None:
        client = _make_client()
        service = TWSQuotesService(client)
        assert await service.snapshot_with_prime([], ["31"]) == []

    async def test_snapshot_skips_unqualifiable_conid(self) -> None:
        contract = _make_contract(conid=42)
        ticker = _make_ticker(conid=42, last=10.0)
        # Erste qualify-Antwort leer, zweite ok.
        client = _make_client(
            qualify_returns=[[], [contract]],
            snapshot_returns=[ticker],
        )
        service = TWSQuotesService(client)
        result = await service.snapshot_with_prime([1, 42], ["31"])
        assert len(result) == 1
        assert result[0].conid == 42

    async def test_snapshot_timeout_skips_quote(self) -> None:
        contract = _make_contract(conid=99)

        async def _slow(*_args: Any, **_kwargs: Any) -> Any:
            await asyncio.sleep(5.0)
            return _make_ticker(conid=99)

        client = _make_client(
            qualify_returns=[[contract]],
            snapshot_side_effect=_slow,
        )
        service = TWSQuotesService(client, snapshot_timeout_s=0.05)
        result = await service.snapshot_with_prime([99], ["31"])
        assert result == []

    async def test_snapshot_market_data_type_realtime(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(conid=265598, last=180.0)
        client = _make_client(
            qualify_returns=[[contract]],
            snapshot_returns=[ticker],
        )
        service = TWSQuotesService(client, market_data_type=1)
        result = await service.snapshot_with_prime([265598], ["31"])
        assert result[0].availability_raw == "RPB"
        assert result[0].availability == "realtime"

    async def test_snapshot_qualify_returns_nested_list(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(conid=265598, last=180.0)
        # ib_async kann gelegentlich list[list[Contract]] liefern.
        async def _qualify(_c: Any) -> Any:
            return [[contract]]

        client = _make_client(
            qualify_side_effect=_qualify,
            snapshot_returns=[ticker],
        )
        service = TWSQuotesService(client)
        result = await service.snapshot_with_prime([265598], ["31"])
        assert len(result) == 1

    async def test_snapshot_qualify_exception_skips(self) -> None:
        async def _qualify(_c: Any) -> Any:
            raise RuntimeError("kaputt")

        client = _make_client(qualify_side_effect=_qualify)
        service = TWSQuotesService(client)
        assert await service.snapshot_with_prime([1, 2], ["31"]) == []


# --------------------------------------------------------------------------
# subscribe(...) (Stream)
# --------------------------------------------------------------------------


class TestSubscribe:
    async def test_subscribe_yields_pushed_events(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(conid=265598, last=None)  # initial leer
        client = _make_client(
            qualify_returns=[[contract]],
            stream_ticker=ticker,
        )
        service = TWSQuotesService(client)
        iterator = await service.subscribe([265598], ["31"], "consumer-a")

        # Update via pendingTickersEvent simulieren
        update = _make_ticker(conid=265598, last=181.0)
        client._ib.pendingTickersEvent.emit([update])

        async def _next_event() -> Any:
            async for event in iterator:
                return event
            return None

        first = await asyncio.wait_for(_next_event(), timeout=1.0)
        assert first is not None
        assert first.conid == 265598
        assert Decimal(first.quote.last) == Decimal("181")

    async def test_subscribe_refcount_shared_ticker(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(conid=265598)
        client = _make_client(
            qualify_returns=[[contract]],
            stream_ticker=ticker,
        )
        service = TWSQuotesService(client)
        await service.subscribe([265598], ["31"], "consumer-a")
        await service.subscribe([265598], ["31"], "consumer-b")

        # reqMktData genau einmal trotz zwei Consumer.
        assert client._ib.reqMktData.call_count == 1
        assert 265598 in service.active_conids

    async def test_subscribe_replay_buffer_for_new_consumer(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(conid=265598)
        client = _make_client(
            qualify_returns=[[contract]],
            stream_ticker=ticker,
        )
        service = TWSQuotesService(client)
        # Erst Consumer A subscriben, dann ein Event pushen, dann Consumer B
        # subscribed - Consumer B muss das gepufferte Event aus dem Replay
        # bekommen.
        await service.subscribe([265598], ["31"], "consumer-a")
        update = _make_ticker(conid=265598, last=200.0)
        client._ib.pendingTickersEvent.emit([update])

        iterator_b = await service.subscribe([265598], ["31"], "consumer-b")

        async def _next() -> Any:
            async for event in iterator_b:
                return event
            return None

        replayed = await asyncio.wait_for(_next(), timeout=1.0)
        assert replayed is not None
        assert Decimal(replayed.quote.last) == Decimal("200")

    async def test_subscribe_replay_filters_by_last_event_id(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(conid=265598)
        client = _make_client(
            qualify_returns=[[contract]],
            stream_ticker=ticker,
        )
        service = TWSQuotesService(client)
        await service.subscribe([265598], ["31"], "consumer-a")
        client._ib.pendingTickersEvent.emit(
            [_make_ticker(conid=265598, last=10.0)]
        )
        client._ib.pendingTickersEvent.emit(
            [_make_ticker(conid=265598, last=11.0)]
        )

        # Consumer B will nur Events nach event_id=0 (also event_id 1).
        iterator_b = await service.subscribe(
            [265598], ["31"], "consumer-b", last_event_id=0
        )

        async def _drain(it: Any, max_count: int = 5) -> list[Any]:
            collected: list[Any] = []
            async for event in it:
                collected.append(event)
                if len(collected) >= max_count:
                    break
            return collected

        # Wir brauchen einen Cancel, weil der Iterator nach Replay
        # weiter auf Live-Events wartet. Der Replay liefert ein Event
        # synchron, das wir hier konsumieren.
        first = await asyncio.wait_for(
            _drain_one(iterator_b), timeout=1.0
        )
        assert first is not None
        assert Decimal(first.quote.last) == Decimal("11")

    async def test_subscribe_empty_conids_raises(self) -> None:
        client = _make_client()
        service = TWSQuotesService(client)
        with pytest.raises(ValueError):
            await service.subscribe([], ["31"], "consumer-a")

    async def test_subscribe_skips_unqualifiable_conid(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(conid=265598)
        client = _make_client(
            qualify_returns=[[], [contract]],
            stream_ticker=ticker,
        )
        service = TWSQuotesService(client)
        await service.subscribe([1, 265598], ["31"], "consumer-a")
        # Nur fuer den qualifizierbaren conid wurde reqMktData aufgerufen.
        assert client._ib.reqMktData.call_count == 1
        assert service.active_conids == {265598}

    async def test_subscribe_cleanup_cancels_market_data(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(conid=265598)
        client = _make_client(
            qualify_returns=[[contract]],
            stream_ticker=ticker,
        )
        service = TWSQuotesService(client)
        iterator = await service.subscribe([265598], ["31"], "consumer-a")
        # Sentinel pushen, damit der Iterator durchlaeuft.
        sub = service._stream_subs[265598]
        for q in list(sub.consumers.values()):
            q.put_nowait(None)
        async for _ in iterator:
            pass
        assert client._ib.cancelMktData.called
        assert 265598 not in service.active_conids

    async def test_listener_attached_only_once(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(conid=265598)
        client = _make_client(
            qualify_returns=[[contract], [contract]],
            stream_ticker=ticker,
        )
        service = TWSQuotesService(client)
        await service.subscribe([265598], ["31"], "a")
        await service.subscribe([265598], ["31"], "b")
        assert client._ib.pendingTickersEvent.listener_count == 1


async def _drain_one(iterator: Any) -> Any:
    async for event in iterator:
        return event
    return None


# --------------------------------------------------------------------------
# shutdown(...)
# --------------------------------------------------------------------------


class TestShutdown:
    async def test_shutdown_cancels_all_and_detaches_listener(self) -> None:
        contract = _make_contract(conid=265598)
        ticker = _make_ticker(conid=265598)
        client = _make_client(
            qualify_returns=[[contract]],
            stream_ticker=ticker,
        )
        service = TWSQuotesService(client)
        await service.subscribe([265598], ["31"], "a")
        assert client._ib.pendingTickersEvent.listener_count == 1
        await service.shutdown()
        assert client._ib.cancelMktData.called
        assert client._ib.pendingTickersEvent.listener_count == 0
        assert service.active_conids == set()

    async def test_shutdown_without_subscribe_is_safe(self) -> None:
        client = _make_client()
        service = TWSQuotesService(client)
        # Kein Listener registriert, kein Crash erwartet.
        await service.shutdown()
        assert service.active_conids == set()


# --------------------------------------------------------------------------
# Helpers (Pure-Function-Tests fuer Field-Mapping)
# --------------------------------------------------------------------------


class TestQuoteFromTicker:
    def test_full_quote_mapping(self) -> None:
        ticker = _make_ticker(
            conid=265598,
            last=180.5,
            bid=180.4,
            ask=180.6,
            volume=129000000.0,
            high=185.0,
            low=178.0,
            close=179.0,
        )
        q = _quote_from_ticker(265598, ticker, market_data_type=1)
        assert q.conid == 265598
        assert Decimal(q.last) == Decimal("180.5")
        assert Decimal(q.bid) == Decimal("180.4")
        assert Decimal(q.ask) == Decimal("180.6")
        assert Decimal(q.high) == Decimal("185")
        assert Decimal(q.low) == Decimal("178")
        assert q.volume == "129000000"
        # Change-% = (180.5 - 179.0) / 179.0 * 100 ≈ 0.84
        assert Decimal(q.change_pct) == Decimal("0.84")
        assert q.availability_raw == "RPB"
        assert q.availability == "realtime"

    def test_nan_values_become_none(self) -> None:
        ticker = _make_ticker(
            conid=1,
            last=float("nan"),
            bid=float("nan"),
            ask=float("nan"),
            volume=float("nan"),
            high=float("nan"),
            low=float("nan"),
        )
        q = _quote_from_ticker(1, ticker, market_data_type=3)
        assert q.last is None
        assert q.bid is None
        assert q.ask is None
        assert q.volume is None
        assert q.high is None
        assert q.low is None

    def test_change_pct_zero_close_returns_none(self) -> None:
        ticker = _make_ticker(conid=1, last=10.0, close=0.0)
        q = _quote_from_ticker(1, ticker, market_data_type=3)
        assert q.change_pct is None

    def test_missing_close_no_change_pct(self) -> None:
        ticker = _make_ticker(conid=1, last=10.0, close=None)
        q = _quote_from_ticker(1, ticker, market_data_type=3)
        assert q.change_pct is None

    def test_unknown_market_data_type(self) -> None:
        ticker = _make_ticker(conid=1, last=1.0)
        q = _quote_from_ticker(1, ticker, market_data_type=99)
        assert q.availability is None
        assert q.availability_raw is None


class TestPureHelpers:
    def test_str_or_none_int(self) -> None:
        assert _str_or_none(5) == "5"

    def test_str_or_none_none(self) -> None:
        assert _str_or_none(None) is None

    def test_str_or_none_nan(self) -> None:
        assert _str_or_none(float("nan")) is None

    def test_str_or_none_invalid_string(self) -> None:
        assert _str_or_none("abc") == "abc"

    def test_volume_negative_returns_none(self) -> None:
        assert _volume_from_ticker(-5) is None

    def test_volume_nan_returns_none(self) -> None:
        assert _volume_from_ticker(float("nan")) is None

    def test_volume_invalid_returns_none(self) -> None:
        assert _volume_from_ticker("not-a-number") is None

    def test_volume_none(self) -> None:
        assert _volume_from_ticker(None) is None

    def test_change_pct_invalid_returns_none(self) -> None:
        ticker = SimpleNamespace(last="abc", close=1.0)
        assert _change_pct_from_ticker(ticker) is None

    def test_change_pct_nan_close(self) -> None:
        ticker = SimpleNamespace(last=10.0, close=float("nan"))
        assert _change_pct_from_ticker(ticker) is None

    def test_availability_unknown_type(self) -> None:
        raw, mapped = _availability_for_market_data_type(99)
        assert raw is None
        assert mapped is None

    def test_ticker_time_datetime_naive(self) -> None:
        dt = datetime(2026, 5, 10, 12, 0, 0)
        result = _ticker_time_to_datetime(dt)
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_ticker_time_datetime_aware(self) -> None:
        dt = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
        result = _ticker_time_to_datetime(dt)
        assert result == dt

    def test_ticker_time_epoch(self) -> None:
        result = _ticker_time_to_datetime(1715342400)
        assert result is not None
        assert result.tzinfo == timezone.utc

    def test_ticker_time_overflow(self) -> None:
        # Sehr grosser Epoch-Wert: erwartet None statt Crash.
        assert _ticker_time_to_datetime(10**20) is None

    def test_ticker_time_none(self) -> None:
        assert _ticker_time_to_datetime(None) is None

    def test_ticker_time_other(self) -> None:
        assert _ticker_time_to_datetime("2026-05-10") is None
