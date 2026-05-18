"""Tests fuer broker_gateway.tws.historical (Karte a5c7ff1c).

Coverage-Ziel: TWSHistoricalService (historical_bars + fundamentals)
plus parse_report_types. Mock-Strategie analog zu test_instruments:
SimpleNamespace fuer Contract/BarData, AsyncMock fuer die ib_async-
Async-Methoden.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from broker_gateway.tws.historical import (
    BAR_SIZE_15MIN,
    BAR_SIZE_DAILY,
    BAR_SIZE_HOURLY,
    DEFAULT_FUNDAMENTAL_REPORTS,
    FundamentalReport,
    HistoricalBarsResponse,
    TWSHistoricalService,
    parse_report_types,
)


# --------------------------------------------------------------------------
# Fixture-Helper
# --------------------------------------------------------------------------


def _make_contract(conid: int = 265598, symbol: str = "AAPL") -> SimpleNamespace:
    return SimpleNamespace(
        conId=conid, symbol=symbol, secType="STK", exchange="SMART", currency="USD"
    )


def _make_bar(
    *,
    day: date,
    open_: float = 100.0,
    high: float = 110.0,
    low: float = 95.0,
    close: float = 105.0,
    volume: int = 1000,
) -> SimpleNamespace:
    return SimpleNamespace(
        date=day,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        average=None,
        barCount=0,
    )


def _make_client(
    *,
    qualify_result: list[SimpleNamespace] | None = None,
    bars: list[SimpleNamespace] | None = None,
    bars_raises: Exception | None = None,
    fundamentals_xml: dict[str, str | Exception] | None = None,
) -> MagicMock:
    contract = qualify_result if qualify_result is not None else [_make_contract()]
    bars_list = bars if bars is not None else []

    qualify = AsyncMock(return_value=list(contract))

    if bars_raises is not None:
        hist = AsyncMock(side_effect=bars_raises)
    else:
        hist = AsyncMock(return_value=list(bars_list))

    fund_map = fundamentals_xml or {}

    async def _fund(_contract: SimpleNamespace, report_type: str) -> str:
        value = fund_map.get(report_type, "")
        if isinstance(value, Exception):
            raise value
        return value

    ib = SimpleNamespace(
        qualifyContractsAsync=qualify,
        reqHistoricalDataAsync=hist,
        reqFundamentalDataAsync=_fund,
    )
    client = MagicMock()
    client._ib = ib
    return client


# --------------------------------------------------------------------------
# parse_report_types
# --------------------------------------------------------------------------


class TestParseReportTypes:
    def test_none_returns_defaults(self) -> None:
        assert parse_report_types(None) == list(DEFAULT_FUNDAMENTAL_REPORTS)

    def test_empty_string_returns_defaults(self) -> None:
        assert parse_report_types("   ") == list(DEFAULT_FUNDAMENTAL_REPORTS)

    def test_comma_separated_with_whitespace(self) -> None:
        assert parse_report_types("ReportSnapshot, RESC ,ReportsFinSummary") == [
            "ReportSnapshot",
            "RESC",
            "ReportsFinSummary",
        ]

    def test_single_value(self) -> None:
        assert parse_report_types("RESC") == ["RESC"]


# --------------------------------------------------------------------------
# historical_bars
# --------------------------------------------------------------------------


class TestHistoricalBars:
    async def test_happy_path_returns_response(self) -> None:
        bars = [
            _make_bar(day=date(2026, 5, 16)),
            _make_bar(day=date(2026, 5, 17), close=106.5),
        ]
        client = _make_client(bars=bars)
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.historical_bars(
            265598, bar_size=BAR_SIZE_DAILY, duration="1 Y"
        )
        assert isinstance(result, HistoricalBarsResponse)
        assert result.conid == 265598
        assert result.bar_size == BAR_SIZE_DAILY
        assert result.duration == "1 Y"
        assert result.use_rth is True
        assert len(result.records) == 2
        first = result.records[0]
        assert first.timestamp.tzinfo == UTC
        assert float(first.close) == 105.0

    async def test_passes_use_rth_false(self) -> None:
        client = _make_client(bars=[_make_bar(day=date(2026, 5, 17))])
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.historical_bars(
            265598,
            bar_size=BAR_SIZE_HOURLY,
            duration="30 D",
            use_rth=False,
        )
        assert result.use_rth is False
        # Check via Mock-Argumente
        call = client._ib.reqHistoricalDataAsync.await_args
        assert call.kwargs["useRTH"] is False
        assert call.kwargs["barSizeSetting"] == BAR_SIZE_HOURLY
        assert call.kwargs["durationStr"] == "30 D"

    async def test_contract_not_found_raises_404(self) -> None:
        client = _make_client(qualify_result=[])
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.historical_bars(
                42, bar_size=BAR_SIZE_DAILY, duration="1 D"
            )
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "contract_not_found"

    async def test_qualify_unwraps_nested_list(self) -> None:
        # ib_async kann manchmal eine Liste-of-Lists liefern (Test in
        # test_tws/test_instruments.py spiegelt das auch wider).
        nested = [[_make_contract(conid=42)]]
        client = _make_client(
            qualify_result=nested, bars=[_make_bar(day=date(2026, 5, 17))]
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.historical_bars(
            42, bar_size=BAR_SIZE_DAILY, duration="1 D"
        )
        assert result.conid == 42

    async def test_pacing_violation_maps_to_429(self) -> None:
        client = _make_client(
            bars_raises=RuntimeError("Error 162: Historical Market Data pacing violation")
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.historical_bars(
                1, bar_size=BAR_SIZE_DAILY, duration="1 D"
            )
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail["code"] == "ibkr_pacing_violation"

    async def test_no_security_definition_maps_to_404(self) -> None:
        client = _make_client(
            bars_raises=RuntimeError("Error 200: No security definition has been found")
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.historical_bars(
                1, bar_size=BAR_SIZE_DAILY, duration="1 D"
            )
        assert exc_info.value.status_code == 404

    async def test_pacing_enforces_min_interval(self) -> None:
        # Min-Abstand 0.05s: erster Call sofort, zweiter wartet 50ms.
        bars = [_make_bar(day=date(2026, 5, 17))]
        client = _make_client(bars=bars)
        service = TWSHistoricalService(client, historical_pacing_s=0.05)

        t0 = time.monotonic()
        await service.historical_bars(
            265598, bar_size=BAR_SIZE_15MIN, duration="7 D"
        )
        await service.historical_bars(
            265598, bar_size=BAR_SIZE_15MIN, duration="7 D"
        )
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.05

    async def test_pacing_serializes_concurrent_calls(self) -> None:
        bars = [_make_bar(day=date(2026, 5, 17))]
        client = _make_client(bars=bars)
        service = TWSHistoricalService(client, historical_pacing_s=0.05)

        t0 = time.monotonic()
        await asyncio.gather(
            service.historical_bars(
                265598, bar_size=BAR_SIZE_DAILY, duration="1 Y"
            ),
            service.historical_bars(
                265598, bar_size=BAR_SIZE_DAILY, duration="1 Y"
            ),
        )
        elapsed = time.monotonic() - t0
        # Beide Calls werden serialisiert; zweiter wartet ~50ms.
        assert elapsed >= 0.05


# --------------------------------------------------------------------------
# fundamentals
# --------------------------------------------------------------------------


class TestFundamentals:
    async def test_happy_path_returns_records(self) -> None:
        client = _make_client(
            fundamentals_xml={
                "ReportSnapshot": "<snapshot>data</snapshot>",
                "RESC": "<resc>data</resc>",
            }
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.fundamentals(
            265598, report_types=["ReportSnapshot", "RESC"]
        )
        assert result.conid == 265598
        assert len(result.records) == 2
        assert result.records[0] == FundamentalReport(
            report_type="ReportSnapshot", xml="<snapshot>data</snapshot>"
        )

    async def test_partial_results_when_some_reports_empty(self) -> None:
        client = _make_client(
            fundamentals_xml={
                "ReportSnapshot": "<snapshot>data</snapshot>",
                "RESC": "",  # leer → wird ausgelassen
            }
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.fundamentals(
            265598, report_types=["ReportSnapshot", "RESC"]
        )
        assert len(result.records) == 1
        assert result.records[0].report_type == "ReportSnapshot"

    async def test_all_empty_raises_404(self) -> None:
        client = _make_client(
            fundamentals_xml={
                "ReportSnapshot": "",
                "RESC": "",
            }
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(
                265598, report_types=["ReportSnapshot", "RESC"]
            )
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "fundamentals_unavailable"

    async def test_reuters_exception_skips_report(self) -> None:
        client = _make_client(
            fundamentals_xml={
                "ReportSnapshot": RuntimeError("Error 430: no reuters subscription"),
                "RESC": "<resc>data</resc>",
            }
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.fundamentals(
            265598, report_types=["ReportSnapshot", "RESC"]
        )
        assert [r.report_type for r in result.records] == ["RESC"]

    async def test_empty_report_types_raises_400(self) -> None:
        client = _make_client()
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(265598, report_types=[])
        assert exc_info.value.status_code == 400

    async def test_unknown_report_type_raises_400(self) -> None:
        client = _make_client()
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(
                265598, report_types=["ReportSnapshot", "InvalidReport"]
            )
        assert exc_info.value.status_code == 400
        assert "InvalidReport" in exc_info.value.detail["message"]

    async def test_fundamentals_timeout_skips_report(self) -> None:
        # ib_async-Stub, der nie returned → asyncio.wait_for-Timeout greift.
        ib_calls: list[str] = []

        async def _slow_fund(_contract: SimpleNamespace, report_type: str) -> str:
            ib_calls.append(report_type)
            await asyncio.sleep(1.0)  # laenger als das Service-Timeout unten
            return "<should-not-arrive />"

        client = MagicMock()
        client._ib = SimpleNamespace(
            qualifyContractsAsync=AsyncMock(return_value=[_make_contract()]),
            reqFundamentalDataAsync=_slow_fund,
        )
        service = TWSHistoricalService(
            client,
            historical_pacing_s=0.0,
            fundamentals_timeout_s=0.05,
        )
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(265598, report_types=["ReportSnapshot"])
        # Kein Reuters-Report kam zurueck → 404 wie bei Berechtigungs-Mangel.
        assert exc_info.value.status_code == 404
        assert ib_calls == ["ReportSnapshot"]
