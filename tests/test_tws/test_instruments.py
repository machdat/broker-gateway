"""Tests fuer broker_gateway.tws.instruments (Karte 50a3ba6a, Phase 2).

Coverage-Ziel: >=90% fuer src/broker_gateway/tws/instruments.py.

Mock-Strategie: TWSClient.``_ib`` wird durch einen SimpleNamespace
ersetzt, der ``reqMatchingSymbolsAsync`` und ``reqContractDetailsAsync``
simuliert. Contract-/ContractDescription-/ContractDetails-Stubs sind
SimpleNamespaces - tests laufen damit ohne echte ib_async-Installation
(obwohl ib_async als dep da ist, brauchen wir es im Test nicht).
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from broker_gateway.tws.instruments import (
    Instrument,
    InstrumentDetail,
    TWSInstrumentsService,
)


# --------------------------------------------------------------------------
# Fixture-Helper
# --------------------------------------------------------------------------


def _make_contract(
    *,
    conid: int,
    symbol: str,
    sec_type: str = "STK",
    primary_exchange: str = "NASDAQ",
    exchange: str = "SMART",
    currency: str = "USD",
) -> SimpleNamespace:
    return SimpleNamespace(
        conId=conid,
        symbol=symbol,
        secType=sec_type,
        primaryExchange=primary_exchange,
        exchange=exchange,
        currency=currency,
    )


def _make_description(
    *, contract: SimpleNamespace, derivative_sec_types: tuple[str, ...] = ()
) -> SimpleNamespace:
    return SimpleNamespace(
        contract=contract,
        derivativeSecTypes=list(derivative_sec_types),
    )


def _make_details(
    *,
    contract: SimpleNamespace,
    long_name: str | None = None,
    trading_hours: str = "",
    liquid_hours: str = "",
    time_zone_id: str = "",
) -> SimpleNamespace:
    # tradingHours/liquidHours/timeZoneId haengen an ContractDetails, nicht
    # am Contract. ib_async-Default ist der leere String (nicht None).
    return SimpleNamespace(
        contract=contract,
        longName=long_name,
        marketName=None,
        tradingHours=trading_hours,
        liquidHours=liquid_hours,
        timeZoneId=time_zone_id,
    )


def _make_client(
    *,
    matching_symbols: list[SimpleNamespace] | None = None,
    contract_details: list[SimpleNamespace] | None = None,
) -> MagicMock:
    descriptions = matching_symbols if matching_symbols is not None else []
    details = contract_details if contract_details is not None else []
    ib = SimpleNamespace(
        reqMatchingSymbolsAsync=AsyncMock(return_value=list(descriptions)),
        reqContractDetailsAsync=AsyncMock(return_value=list(details)),
    )
    client = MagicMock()
    client._ib = ib
    return client


# --------------------------------------------------------------------------
# search(...)
# --------------------------------------------------------------------------


class TestSearch:
    async def test_search_returns_primary_stk(self) -> None:
        descriptions = [
            _make_description(
                contract=_make_contract(
                    conid=265598,
                    symbol="AAPL",
                    sec_type="STK",
                    primary_exchange="NASDAQ",
                )
            ),
            _make_description(
                contract=_make_contract(
                    conid=999,
                    symbol="AAPL",
                    sec_type="OPT",
                )
            ),
        ]
        client = _make_client(matching_symbols=descriptions)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        result = await service.search("AAPL")
        assert len(result) == 1
        assert isinstance(result[0], Instrument)
        assert result[0].conid == 265598
        assert result[0].symbol == "AAPL"
        assert result[0].sec_type == "STK"
        assert result[0].currency == "USD"

    async def test_search_filters_by_exchange(self) -> None:
        descriptions = [
            _make_description(
                contract=_make_contract(
                    conid=1,
                    symbol="X",
                    primary_exchange="NYSE",
                )
            ),
            _make_description(
                contract=_make_contract(
                    conid=2,
                    symbol="X",
                    primary_exchange="NASDAQ",
                )
            ),
        ]
        client = _make_client(matching_symbols=descriptions)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        result = await service.search("X", exchange="NASDAQ")
        # Beide STKs - cp-Variante reduziert auf primary_stk (erste Treffer
        # nach Filter); Ergebnis: 1 Eintrag, conid=2.
        assert len(result) == 1
        assert result[0].conid == 2

    async def test_search_returns_cached_result(self) -> None:
        descriptions = [
            _make_description(
                contract=_make_contract(conid=1, symbol="AAPL")
            )
        ]
        client = _make_client(matching_symbols=descriptions)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        await service.search("AAPL")
        await service.search("AAPL")
        # Zweiter Call darf kein zweites reqMatchingSymbolsAsync ausloesen
        assert client._ib.reqMatchingSymbolsAsync.await_count == 1

    async def test_search_empty_symbol_raises_400(self) -> None:
        client = _make_client()
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.search("   ")
        assert exc_info.value.status_code == 400

    async def test_search_skips_descriptions_without_conid(self) -> None:
        descriptions = [
            SimpleNamespace(
                contract=SimpleNamespace(conId=None, symbol="BAD"),
                derivativeSecTypes=[],
            ),
            _make_description(
                contract=_make_contract(conid=1, symbol="OK"),
            ),
        ]
        client = _make_client(matching_symbols=descriptions)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        result = await service.search("X")
        assert len(result) == 1
        assert result[0].conid == 1

    async def test_search_rate_limit_serializes_calls(self) -> None:
        # Zwei parallele Calls duerfen nicht gleichzeitig laufen
        descriptions = [
            _make_description(
                contract=_make_contract(conid=1, symbol="A"),
            )
        ]
        client = _make_client(matching_symbols=descriptions)
        service = TWSInstrumentsService(
            client, rate_limit_s=0.05
        )

        async def _call(symbol: str) -> Any:
            return await service.search(symbol)

        # Zwei verschiedene Symbole → Cache umgeht das nicht
        results = await asyncio.gather(_call("A"), _call("B"))
        assert len(results) == 2
        # Lock + Sleep haben min ~0.05s gewartet zwischen den Calls
        assert client._ib.reqMatchingSymbolsAsync.await_count == 2


# --------------------------------------------------------------------------
# search_by_isin(...)
# --------------------------------------------------------------------------


class TestSearchByIsin:
    async def test_search_by_isin_invalid_format_raises_422(self) -> None:
        client = _make_client()
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.search_by_isin("INVALID")
        assert exc_info.value.status_code == 422

    async def test_search_by_isin_returns_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Contract-Stub-Klasse, damit der Adapter ohne ib_async-Import
        # funktioniert. Wir patchen direkt die _import_contract_class-
        # Methode auf eine Stub-Klasse.
        contract_calls: list[dict[str, Any]] = []

        class _Contract:
            def __init__(self, **kwargs: Any) -> None:
                contract_calls.append(kwargs)
                for k, v in kwargs.items():
                    setattr(self, k, v)

        details = [
            _make_details(
                contract=_make_contract(
                    conid=755111,
                    symbol="SAP",
                    primary_exchange="IBIS",
                ),
                long_name="SAP SE",
            ),
        ]
        client = _make_client(contract_details=details)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)

        async def _fake_import(_self: Any) -> Any:
            return _Contract

        monkeypatch.setattr(
            TWSInstrumentsService,
            "_import_contract_class",
            _fake_import,
        )
        result = await service.search_by_isin("DE0007164600")
        assert len(result) == 1
        assert result[0].conid == 755111
        assert result[0].isin == "DE0007164600"
        assert result[0].company_name == "SAP SE"
        # Probe-Contract wurde mit ISIN-Felder gebaut
        assert contract_calls[0]["secIdType"] == "ISIN"
        assert contract_calls[0]["secId"] == "DE0007164600"
        assert contract_calls[0]["secType"] == "STK"

    async def test_search_by_isin_filters_by_mic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Contract:
            def __init__(self, **kwargs: Any) -> None:
                for k, v in kwargs.items():
                    setattr(self, k, v)

        details = [
            _make_details(
                contract=_make_contract(
                    conid=1, symbol="SAP", primary_exchange="IBIS"
                )
            ),
            _make_details(
                contract=_make_contract(
                    conid=2, symbol="SAP", primary_exchange="NYSE"
                )
            ),
        ]
        client = _make_client(contract_details=details)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)

        async def _fake_import(_self: Any) -> Any:
            return _Contract

        monkeypatch.setattr(
            TWSInstrumentsService, "_import_contract_class", _fake_import
        )
        result = await service.search_by_isin("DE0007164600", mic="NYSE")
        assert len(result) == 1
        assert result[0].conid == 2

    async def test_search_by_isin_caches_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Contract:
            def __init__(self, **kwargs: Any) -> None:
                for k, v in kwargs.items():
                    setattr(self, k, v)

        details = [
            _make_details(
                contract=_make_contract(conid=1, symbol="SAP")
            )
        ]
        client = _make_client(contract_details=details)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)

        async def _fake_import(_self: Any) -> Any:
            return _Contract

        monkeypatch.setattr(
            TWSInstrumentsService, "_import_contract_class", _fake_import
        )
        await service.search_by_isin("DE0007164600")
        await service.search_by_isin("DE0007164600")
        assert client._ib.reqContractDetailsAsync.await_count == 1


# --------------------------------------------------------------------------
# info(conid)
# --------------------------------------------------------------------------


class TestInfo:
    async def test_info_returns_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Contract:
            def __init__(self, **kwargs: Any) -> None:
                for k, v in kwargs.items():
                    setattr(self, k, v)

        details = [
            _make_details(
                contract=_make_contract(
                    conid=265598,
                    symbol="AAPL",
                    primary_exchange="NASDAQ",
                ),
                long_name="Apple Inc.",
            )
        ]
        client = _make_client(contract_details=details)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)

        async def _fake_import(_self: Any) -> Any:
            return _Contract

        monkeypatch.setattr(
            TWSInstrumentsService, "_import_contract_class", _fake_import
        )
        result = await service.info(265598)
        assert isinstance(result, InstrumentDetail)
        assert result.conid == 265598
        assert result.symbol == "AAPL"
        assert result.exchange_id == "NASDAQ"
        assert result.calendar_url == "/v1/exchanges/NASDAQ/calendar"
        assert result.company_name == "Apple Inc."

    async def test_info_404_when_no_details(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Contract:
            def __init__(self, **kwargs: Any) -> None:
                for k, v in kwargs.items():
                    setattr(self, k, v)

        client = _make_client(contract_details=[])
        service = TWSInstrumentsService(client, rate_limit_s=0.0)

        async def _fake_import(_self: Any) -> Any:
            return _Contract

        monkeypatch.setattr(
            TWSInstrumentsService, "_import_contract_class", _fake_import
        )
        with pytest.raises(HTTPException) as exc_info:
            await service.info(999999)
        assert exc_info.value.status_code == 404

    async def test_info_caches_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Contract:
            def __init__(self, **kwargs: Any) -> None:
                for k, v in kwargs.items():
                    setattr(self, k, v)

        details = [
            _make_details(
                contract=_make_contract(conid=1, symbol="AAPL")
            )
        ]
        client = _make_client(contract_details=details)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)

        async def _fake_import(_self: Any) -> Any:
            return _Contract

        monkeypatch.setattr(
            TWSInstrumentsService, "_import_contract_class", _fake_import
        )
        await service.info(1)
        await service.info(1)
        assert client._ib.reqContractDetailsAsync.await_count == 1

    async def test_info_502_when_contract_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _Contract:
            def __init__(self, **kwargs: Any) -> None:
                for k, v in kwargs.items():
                    setattr(self, k, v)

        # ContractDetails ohne contract-Feld - defensive Pfad
        broken = SimpleNamespace(contract=None, longName=None)
        client = _make_client(contract_details=[broken])
        service = TWSInstrumentsService(client, rate_limit_s=0.0)

        async def _fake_import(_self: Any) -> Any:
            return _Contract

        monkeypatch.setattr(
            TWSInstrumentsService, "_import_contract_class", _fake_import
        )
        with pytest.raises(HTTPException) as exc_info:
            await service.info(1)
        assert exc_info.value.status_code == 502

    async def test_info_passes_through_trading_hours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Karte f1a01d97: tradingHours/liquidHours/timeZoneId werden
        additiv und verlustfrei in InstrumentDetail durchgereicht."""

        class _Contract:
            def __init__(self, **kwargs: Any) -> None:
                for k, v in kwargs.items():
                    setattr(self, k, v)

        trading = "20260628:CLOSED;20260629:0400-20260629:2000"
        liquid = "20260628:CLOSED;20260629:0930-20260629:1600"
        details = [
            _make_details(
                contract=_make_contract(
                    conid=265598, symbol="AAPL", primary_exchange="NASDAQ"
                ),
                long_name="Apple Inc.",
                trading_hours=trading,
                liquid_hours=liquid,
                time_zone_id="US/Eastern",
            )
        ]
        client = _make_client(contract_details=details)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)

        async def _fake_import(_self: Any) -> Any:
            return _Contract

        monkeypatch.setattr(
            TWSInstrumentsService, "_import_contract_class", _fake_import
        )
        result = await service.info(265598)
        assert result.trading_hours == trading
        assert result.liquid_hours == liquid
        assert result.time_zone_id == "US/Eastern"

    async def test_info_empty_hours_become_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ib_async-Default ist der leere String - der v1-Contract soll
        stattdessen None liefern (= 'nicht geliefert')."""

        class _Contract:
            def __init__(self, **kwargs: Any) -> None:
                for k, v in kwargs.items():
                    setattr(self, k, v)

        # _make_details ohne Hours-Argumente -> Default "" (wie ib_async)
        details = [
            _make_details(
                contract=_make_contract(conid=1, symbol="AAPL")
            )
        ]
        client = _make_client(contract_details=details)
        service = TWSInstrumentsService(client, rate_limit_s=0.0)

        async def _fake_import(_self: Any) -> Any:
            return _Contract

        monkeypatch.setattr(
            TWSInstrumentsService, "_import_contract_class", _fake_import
        )
        result = await service.info(1)
        assert result.trading_hours is None
        assert result.liquid_hours is None
        assert result.time_zone_id is None


# --------------------------------------------------------------------------
# Cache-Properties
# --------------------------------------------------------------------------


class TestCacheProperties:
    def test_search_cache_exposed(self) -> None:
        client = _make_client()
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        assert service.search_cache is service._search_cache
        assert service.isin_cache is service._isin_cache
        assert service.info_cache is service._info_cache


# --------------------------------------------------------------------------
# Hours-TTL (Karte 35162b2d)
# --------------------------------------------------------------------------


class TestHoursTtl:
    """Der Hours-tragende info-Cache hängt an einer eigenen kurzen TTL.

    IBKR liefert die Contract-Hours als rollierendes Fenster von 4
    Handelstagen (Messung in docs/api/v1.md 4.2). Mit der alten 7-Tage-TTL
    enthielt ein Cache-Eintrag nach wenigen Tagen kein Segment für den
    aktuellen Tag mehr.
    """

    def test_info_cache_ttl_is_short_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BG_INSTRUMENT_HOURS_TTL_S", raising=False)
        client = _make_client()
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        assert service.hours_ttl_seconds == 3600.0
        assert service.info_cache.ttl_seconds == 3600.0

    def test_mapping_caches_keep_the_long_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BG_INSTRUMENT_HOURS_TTL_S", raising=False)
        client = _make_client()
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        seven_days = 7 * 24 * 60 * 60
        assert service.search_cache.ttl_seconds == seven_days
        assert service.isin_cache.ttl_seconds == seven_days
        # Der Hours-Pfad darf gerade NICHT daran hängen.
        assert service.info_cache.ttl_seconds < seven_days

    def test_hours_ttl_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BG_INSTRUMENT_HOURS_TTL_S", "120")
        client = _make_client()
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        assert service.hours_ttl_seconds == 120.0

    @pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "nonsense"])
    def test_hours_ttl_env_garbage_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        # Eine vertippte ENV darf den Service nicht am Start hindern.
        monkeypatch.setenv("BG_INSTRUMENT_HOURS_TTL_S", raw)
        client = _make_client()
        service = TWSInstrumentsService(client, rate_limit_s=0.0)
        assert service.hours_ttl_seconds == 3600.0

    def test_explicit_argument_beats_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BG_INSTRUMENT_HOURS_TTL_S", "120")
        client = _make_client()
        service = TWSInstrumentsService(
            client, hours_ttl_seconds=30.0, rate_limit_s=0.0
        )
        assert service.hours_ttl_seconds == 30.0

    async def test_expired_hours_refetched_while_mapping_stays_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verification-Kriterium der Karte, wörtlich.

        Ein Hours-Eintrag älter als die neue TTL wird neu geholt, während
        das conid/symbol-Mapping weiter aus dem Cache kommt.
        """

        class _Contract:
            def __init__(self, **kwargs: Any) -> None:
                for k, v in kwargs.items():
                    setattr(self, k, v)

        contract = _make_contract(conid=265598, symbol="AAPL")
        client = _make_client(
            matching_symbols=[_make_description(contract=contract)],
            contract_details=[
                _make_details(
                    contract=contract,
                    trading_hours="20260714:0400-20260714:2000",
                    liquid_hours="20260714:0930-20260714:1600",
                    time_zone_id="US/Eastern",
                )
            ],
        )
        service = TWSInstrumentsService(
            client, hours_ttl_seconds=0.05, rate_limit_s=0.0
        )

        async def _fake_import(_self: Any) -> Any:
            return _Contract

        monkeypatch.setattr(
            TWSInstrumentsService, "_import_contract_class", _fake_import
        )

        await service.search("AAPL")
        await service.info(265598)
        assert client._ib.reqContractDetailsAsync.await_count == 1

        await asyncio.sleep(0.06)

        # Hours abgelaufen -> neuer IBKR-Call.
        await service.info(265598)
        assert client._ib.reqContractDetailsAsync.await_count == 2
        # conid/symbol-Mapping hängt an der 7-Tage-TTL und bleibt gecacht.
        await service.search("AAPL")
        assert client._ib.reqMatchingSymbolsAsync.await_count == 1
