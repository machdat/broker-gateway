"""Tests fuer broker_gateway.tws.scanner (RW-07 / Karte Scanner-Endpunkt).

Coverage-Ziel: TWSScannerService (scan + scanner_parameters) plus die
Parsing-/Validierungs-Helfer parse_filter_options + validate_number_of_rows.
Mock-Strategie analog zu test_historical: SimpleNamespace fuer die
ib_async-ScanData-Objekte, AsyncMock bzw. eigene Coroutine fuer die
ib_async-Async-Methoden. Keine Live-TWS-Session noetig.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from ib_async.contract import TagValue

from broker_gateway.tws.scanner import (
    DEFAULT_INSTRUMENT,
    DEFAULT_LOCATION_CODE,
    MAX_CONCURRENT_SCANS,
    MAX_ROWS,
    ScannerParametersResponse,
    ScannerResultsResponse,
    ScanRow,
    TWSScannerService,
    parse_filter_options,
    validate_number_of_rows,
)


# --------------------------------------------------------------------------
# Fixture-Helper
# --------------------------------------------------------------------------


def _make_scan_data(
    *,
    rank: int = 0,
    conid: int = 265598,
    symbol: str = "AAPL",
    sec_type: str = "STK",
    exchange: str = "SMART",
    primary_exchange: str = "NASDAQ",
    currency: str = "USD",
    distance: str = "",
    benchmark: str = "",
    projection: str = "",
    legs_str: str = "",
) -> SimpleNamespace:
    contract = SimpleNamespace(
        conId=conid,
        symbol=symbol,
        secType=sec_type,
        exchange=exchange,
        primaryExchange=primary_exchange,
        currency=currency,
    )
    contract_details = SimpleNamespace(contract=contract)
    return SimpleNamespace(
        rank=rank,
        contractDetails=contract_details,
        distance=distance,
        benchmark=benchmark,
        projection=projection,
        legsStr=legs_str,
    )


def _make_client(
    *,
    scan_result: list[SimpleNamespace] | None = None,
    scan_raises: Exception | None = None,
    parameters_xml: str = "<ScanParameterResponse/>",
) -> MagicMock:
    if scan_raises is not None:
        scan = AsyncMock(side_effect=scan_raises)
    else:
        scan = AsyncMock(return_value=list(scan_result or []))
    ib = SimpleNamespace(
        reqScannerDataAsync=scan,
        reqScannerParametersAsync=AsyncMock(return_value=parameters_xml),
    )
    client = MagicMock()
    client._ib = ib
    return client


# --------------------------------------------------------------------------
# parse_filter_options
# --------------------------------------------------------------------------


class TestParseFilterOptions:
    def test_none_returns_empty(self) -> None:
        assert parse_filter_options(None) == []

    def test_empty_list_returns_empty(self) -> None:
        assert parse_filter_options([]) == []

    def test_parses_tag_value_pairs(self) -> None:
        result = parse_filter_options(["peRatioBelow:20", "marketCapAbove1e6:500"])
        assert result == [("peRatioBelow", "20"), ("marketCapAbove1e6", "500")]

    def test_tolerates_surrounding_whitespace(self) -> None:
        assert parse_filter_options([" peRatioBelow : 20 "]) == [("peRatioBelow", "20")]

    def test_value_may_contain_colon(self) -> None:
        # Nur am ERSTEN Doppelpunkt splitten - Werte duerfen ':' enthalten.
        assert parse_filter_options(["someTag:a:b"]) == [("someTag", "a:b")]

    def test_missing_colon_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            parse_filter_options(["noColonHere"])
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "invalid_filter_option"

    def test_empty_tag_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            parse_filter_options([":20"])
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "invalid_filter_option"

    def test_empty_value_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            parse_filter_options(["peRatioBelow:"])
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "invalid_filter_option"


# --------------------------------------------------------------------------
# validate_number_of_rows
# --------------------------------------------------------------------------


class TestValidateNumberOfRows:
    def test_accepts_within_range(self) -> None:
        assert validate_number_of_rows(50) == 50
        assert validate_number_of_rows(1) == 1

    def test_max_rows_is_50(self) -> None:
        assert MAX_ROWS == 50

    def test_rejects_above_max_with_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_number_of_rows(51)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "invalid_number_of_rows"

    def test_rejects_below_one_with_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_number_of_rows(0)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "invalid_number_of_rows"


# --------------------------------------------------------------------------
# scan - happy path + Mapping
# --------------------------------------------------------------------------


class TestScan:
    async def test_happy_path_maps_scan_rows(self) -> None:
        client = _make_client(
            scan_result=[
                _make_scan_data(rank=0, conid=265598, symbol="AAPL"),
                _make_scan_data(
                    rank=1, conid=272093, symbol="MSFT", primary_exchange="NASDAQ"
                ),
            ]
        )
        service = TWSScannerService(client)
        result = await service.scan(scan_code="TOP_PERC_GAIN")
        assert isinstance(result, ScannerResultsResponse)
        assert result.scan_code == "TOP_PERC_GAIN"
        assert result.instrument == DEFAULT_INSTRUMENT
        assert result.location_code == DEFAULT_LOCATION_CODE
        assert len(result.results) == 2
        first = result.results[0]
        assert isinstance(first, ScanRow)
        assert first.rank == 0
        assert first.con_id == 265598
        assert first.symbol == "AAPL"
        assert first.sec_type == "STK"
        assert first.primary_exchange == "NASDAQ"
        assert first.currency == "USD"

    async def test_empty_result_returns_empty_list(self) -> None:
        client = _make_client(scan_result=[])
        service = TWSScannerService(client)
        result = await service.scan(scan_code="TOP_PERC_GAIN")
        assert result.results == []

    async def test_builds_subscription_from_params(self) -> None:
        client = _make_client(scan_result=[])
        service = TWSScannerService(client)
        await service.scan(
            scan_code="HIGH_DIVIDEND_YIELD",
            instrument="STK",
            location_code="STK.EU",
            number_of_rows=25,
        )
        call = client._ib.reqScannerDataAsync.await_args
        sub = call.args[0]
        assert sub.scanCode == "HIGH_DIVIDEND_YIELD"
        assert sub.instrument == "STK"
        assert sub.locationCode == "STK.EU"
        assert sub.numberOfRows == 25

    async def test_sets_optional_base_filters(self) -> None:
        client = _make_client(scan_result=[])
        service = TWSScannerService(client)
        await service.scan(
            scan_code="TOP_PERC_GAIN",
            above_price=10.0,
            below_price=500.0,
            above_volume=1_000_000,
            market_cap_above=5_000_000_000.0,
            market_cap_below=None,
            stock_type_filter="CORP",
        )
        sub = client._ib.reqScannerDataAsync.await_args.args[0]
        assert sub.abovePrice == 10.0
        assert sub.belowPrice == 500.0
        assert sub.aboveVolume == 1_000_000
        assert sub.marketCapAbove == 5_000_000_000.0
        assert sub.stockTypeFilter == "CORP"

    async def test_passes_fundamental_filter_options_as_tagvalues(self) -> None:
        # Verification #1: Fundamental-Ratio-Filter laufen ueber
        # scannerSubscriptionFilterOptions als Liste von TagValue.
        client = _make_client(scan_result=[])
        service = TWSScannerService(client)
        await service.scan(
            scan_code="TOP_PERC_GAIN",
            filter_options=[("peRatioBelow", "20"), ("marketCapAbove1e6", "500")],
        )
        call = client._ib.reqScannerDataAsync.await_args
        sent = call.kwargs["scannerSubscriptionFilterOptions"]
        assert sent == [
            TagValue("peRatioBelow", "20"),
            TagValue("marketCapAbove1e6", "500"),
        ]

    async def test_no_filter_options_sends_empty_list(self) -> None:
        client = _make_client(scan_result=[])
        service = TWSScannerService(client)
        await service.scan(scan_code="TOP_PERC_GAIN")
        call = client._ib.reqScannerDataAsync.await_args
        assert call.kwargs["scannerSubscriptionFilterOptions"] == []

    async def test_scanned_at_is_utc(self) -> None:
        client = _make_client(scan_result=[])
        service = TWSScannerService(client)
        result = await service.scan(scan_code="TOP_PERC_GAIN")
        assert isinstance(result.scanned_at, datetime)
        assert result.scanned_at.tzinfo == UTC

    async def test_rejects_number_of_rows_above_50(self) -> None:
        client = _make_client(scan_result=[])
        service = TWSScannerService(client)
        with pytest.raises(HTTPException) as exc_info:
            await service.scan(scan_code="TOP_PERC_GAIN", number_of_rows=51)
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "invalid_number_of_rows"
        # Bei Validierungsfehler darf IBKR gar nicht erst gerufen werden.
        client._ib.reqScannerDataAsync.assert_not_awaited()

    async def test_pacing_violation_maps_to_429(self) -> None:
        client = _make_client(
            scan_raises=RuntimeError("Error 162: market data pacing violation")
        )
        service = TWSScannerService(client)
        with pytest.raises(HTTPException) as exc_info:
            await service.scan(scan_code="TOP_PERC_GAIN")
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail["code"] == "ibkr_pacing_violation"

    async def test_invalid_scan_code_maps_to_422(self) -> None:
        client = _make_client(
            scan_raises=RuntimeError("Error 10: Invalid scan code NOT_A_CODE")
        )
        service = TWSScannerService(client)
        with pytest.raises(HTTPException) as exc_info:
            await service.scan(scan_code="NOT_A_CODE")
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "invalid_scan_request"

    async def test_unknown_error_maps_to_502(self) -> None:
        client = _make_client(scan_raises=RuntimeError("something exploded"))
        service = TWSScannerService(client)
        with pytest.raises(HTTPException) as exc_info:
            await service.scan(scan_code="TOP_PERC_GAIN")
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["code"] == "ib_async_error"


# --------------------------------------------------------------------------
# Concurrency-Grenze (Verification #3)
# --------------------------------------------------------------------------


class TestConcurrencyLimit:
    def test_max_concurrent_scans_is_10(self) -> None:
        assert MAX_CONCURRENT_SCANS == 10

    async def test_enforces_max_concurrent_scans(self) -> None:
        # Ein "langsamer" Scan misst, wie viele Scans gleichzeitig aktiv
        # sind. Die Semaphore darf die konfigurierte Grenze nie ueberschreiten.
        concurrent = 0
        max_seen = 0

        async def _slow_scan(_sub: object, **_kw: object) -> list[SimpleNamespace]:
            nonlocal concurrent, max_seen
            concurrent += 1
            max_seen = max(max_seen, concurrent)
            await asyncio.sleep(0.02)
            concurrent -= 1
            return []

        client = MagicMock()
        client._ib = SimpleNamespace(reqScannerDataAsync=_slow_scan)
        service = TWSScannerService(client, max_concurrent_scans=3)

        await asyncio.gather(
            *[service.scan(scan_code="TOP_PERC_GAIN") for _ in range(9)]
        )
        assert max_seen == 3


# --------------------------------------------------------------------------
# scanner_parameters
# --------------------------------------------------------------------------


class TestScannerParameters:
    async def test_returns_xml(self) -> None:
        client = _make_client(parameters_xml="<ScanParameterResponse>xml</ScanParameterResponse>")
        service = TWSScannerService(client)
        result = await service.scanner_parameters()
        assert isinstance(result, ScannerParametersResponse)
        assert result.xml == "<ScanParameterResponse>xml</ScanParameterResponse>"

    async def test_maps_error_to_502(self) -> None:
        client = _make_client()
        client._ib.reqScannerParametersAsync = AsyncMock(
            side_effect=RuntimeError("boom")
        )
        service = TWSScannerService(client)
        with pytest.raises(HTTPException) as exc_info:
            await service.scanner_parameters()
        assert exc_info.value.status_code == 502
