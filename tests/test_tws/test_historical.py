"""Tests fuer broker_gateway.tws.historical (Karte a5c7ff1c).

Coverage-Ziel: TWSHistoricalService (historical_bars + fundamentals)
plus parse_report_types. Mock-Strategie analog zu test_instruments:
SimpleNamespace fuer Contract/BarData, AsyncMock fuer die ib_async-
Async-Methoden.
"""
from __future__ import annotations

import asyncio
import itertools
import time
from datetime import UTC, date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from eventkit import Event
from fastapi import HTTPException

from broker_gateway.tws.historical import (
    ALLOWED_WHAT_TO_SHOW,
    BAR_SIZE_15MIN,
    BAR_SIZE_DAILY,
    BAR_SIZE_HOURLY,
    DEFAULT_FUNDAMENTAL_REPORTS,
    DEFAULT_WHAT_TO_SHOW,
    FundamentalReport,
    HistoricalBarsResponse,
    TWSHistoricalService,
    parse_report_types,
    validate_what_to_show,
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


class _FakeBarList(list):
    """Simuliert ib_async.BarDataList: eine Liste mit reqId-Attribut.

    ib_async setzt ``bars.reqId`` beim Request und loest das Future bei
    RaiseRequestErrors=False auch im Fehlerfall mit dieser (leeren) Liste
    auf - die reqId bleibt fuer die errorEvent-Zuordnung erhalten.
    """

    reqId: int


def _make_client(
    *,
    qualify_result: list[SimpleNamespace] | None = None,
    bars: list[SimpleNamespace] | None = None,
    bars_raises: Exception | None = None,
    hist_req_id: int = 1,
    hist_error_events: list[tuple[int, int, str]] | None = None,
) -> MagicMock:
    """Baut einen ib-Handle-Fake mit echtem errorEvent.

    ``hist_error_events`` ist eine Liste von (reqId, errorCode, errorString),
    die reqHistoricalDataAsync waehrend des Calls ueber errorEvent emittiert -
    so wie der echte ib_async-Wrapper (wrapper.error() -> errorEvent.emit).
    Der Call liefert dann - genau wie ib_async bei RaiseRequestErrors=False -
    trotzdem eine (leere) BarDataList mit reqId zurueck, statt zu werfen.
    """
    contract = qualify_result if qualify_result is not None else [_make_contract()]
    bars_list = bars if bars is not None else []

    qualify = AsyncMock(return_value=list(contract))

    ib = SimpleNamespace(errorEvent=Event("errorEvent"))

    result_bars = _FakeBarList(bars_list)
    result_bars.reqId = hist_req_id

    async def _hist(_contract: SimpleNamespace, **_kw: object) -> _FakeBarList:
        if bars_raises is not None:
            raise bars_raises
        for reqid, code, msg in (hist_error_events or []):
            ib.errorEvent.emit(reqid, code, msg, None)
        return result_bars

    ib.qualifyContractsAsync = qualify
    ib.reqHistoricalDataAsync = AsyncMock(side_effect=_hist)

    client = MagicMock()
    client._ib = ib
    return client


# Sentinels fuer den Fundamentals-Fake:
#   _FUND_TIMEOUT         -> Future bleibt offen (asyncio.wait_for-Timeout)
#   _FUND_EMPTY_CONTAINER -> Future loest mit leerem Default-Container [] auf,
#                            OHNE fatales errorEvent (kann real nur ueber einen
#                            Bug entstehen; testet den isinstance(raw,str)-Guard)
_FUND_TIMEOUT = "__TIMEOUT__"
_FUND_EMPTY_CONTAINER = "__EMPTY_CONTAINER__"


def _make_fund_client(
    *,
    qualify_result: list[SimpleNamespace] | None = None,
    fundamentals: dict[str, str | tuple[int, str] | BaseException] | None = None,
    fund_start_req_id: int = 100,
    foreign_error_event: tuple[int, int, str] | None = None,
    own_error_events: dict[str, list[tuple[int, str]]] | None = None,
) -> MagicMock:
    """ib-Handle-Fake, der den ib_async-Low-Level-Fundamentals-Pfad nachbaut.

    Der Historik-Service holt Fundamentals nicht ueber das High-Level
    ``reqFundamentalDataAsync`` (dessen str-Ergebnis traegt keine reqId),
    sondern ueber ``client.getReqId`` + ``wrapper.startReq`` +
    ``client.reqFundamentalData`` - so bleibt die reqId fuer die
    errorEvent-Zuordnung bekannt. Dieser Fake bildet genau das ab.

    ``fundamentals`` bildet report_type auf ein Outcome ab:
      - ``str``            -> Erfolg, Future wird mit dem String aufgeloest
      - ``(code, msg)``    -> IBKR-Reject: errorEvent(code) + Future mit ``[]``
                              aufgeloest (wie ib_async _endReq bei
                              RaiseRequestErrors=False)
      - ``BaseException``  -> Future wird via set_exception aufgeloest (wie
                              ib_async connectionClosed -> ConnectionError auf
                              pending Futures)
      - :data:`_FUND_TIMEOUT`         -> Future bleibt offen (Timeout greift)
      - :data:`_FUND_EMPTY_CONTAINER` -> Future mit [] aufgeloest, kein Event

    ``own_error_events`` emittiert je report_type zusaetzliche Events mit der
    EIGENEN reqId (z.B. benigne Warnings neben Daten). ``foreign_error_event``
    emittiert ein Event mit fremder reqId (Cross-Talk-Test).
    """
    contract = qualify_result if qualify_result is not None else [_make_contract()]
    qualify = AsyncMock(return_value=list(contract))
    fund_map = fundamentals or {}

    ib = SimpleNamespace(errorEvent=Event("errorEvent"))
    ib.qualifyContractsAsync = qualify

    futures: dict[int, asyncio.Future] = {}
    counter = itertools.count(fund_start_req_id)
    calls: list[str] = []

    def _get_req_id() -> int:
        return next(counter)

    def _start_req(req_id: int, contract: object = None) -> asyncio.Future:
        fut: asyncio.Future = asyncio.Future()
        futures[req_id] = fut
        return fut

    def _req_fundamental(
        req_id: int, contract: object, report_type: str, opts: object
    ) -> None:
        calls.append(report_type)
        outcome = fund_map.get(report_type, "")
        fut = futures[req_id]
        if foreign_error_event is not None:
            ib.errorEvent.emit(*foreign_error_event, None)
        for code, msg in (own_error_events or {}).get(report_type, []):
            ib.errorEvent.emit(req_id, code, msg, contract)
        if isinstance(outcome, BaseException):
            if not fut.done():
                fut.set_exception(outcome)
        elif isinstance(outcome, tuple):
            code, msg = outcome
            ib.errorEvent.emit(req_id, code, msg, contract)
            if not fut.done():
                fut.set_result([])
        elif outcome == _FUND_EMPTY_CONTAINER:
            if not fut.done():
                fut.set_result([])
        elif outcome == _FUND_TIMEOUT:
            return
        else:
            if not fut.done():
                fut.set_result(outcome)

    ib.client = SimpleNamespace(
        getReqId=_get_req_id, reqFundamentalData=_req_fundamental
    )
    ib.wrapper = SimpleNamespace(startReq=_start_req)

    client = MagicMock()
    client._ib = ib
    client._fund_calls = calls
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
# validate_what_to_show
# --------------------------------------------------------------------------


class TestValidateWhatToShow:
    def test_default_is_trades_and_whitelisted(self) -> None:
        assert DEFAULT_WHAT_TO_SHOW == "TRADES"
        assert DEFAULT_WHAT_TO_SHOW in ALLOWED_WHAT_TO_SHOW

    def test_accepts_trades(self) -> None:
        assert validate_what_to_show("TRADES") == "TRADES"

    def test_accepts_adjusted_last(self) -> None:
        assert validate_what_to_show("ADJUSTED_LAST") == "ADJUSTED_LAST"

    def test_rejects_unknown_with_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_what_to_show("FOO")
        assert exc_info.value.status_code == 422
        assert exc_info.value.detail["code"] == "unsupported_what_to_show"


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

    async def test_default_what_to_show_is_trades(self) -> None:
        client = _make_client(bars=[_make_bar(day=date(2026, 5, 17))])
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.historical_bars(
            265598, bar_size=BAR_SIZE_DAILY, duration="1 Y"
        )
        assert result.what_to_show == "TRADES"
        call = client._ib.reqHistoricalDataAsync.await_args
        assert call.kwargs["whatToShow"] == "TRADES"

    async def test_passes_what_to_show_adjusted_last(self) -> None:
        client = _make_client(bars=[_make_bar(day=date(2026, 5, 17))])
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.historical_bars(
            265598,
            bar_size=BAR_SIZE_DAILY,
            duration="1 Y",
            what_to_show="ADJUSTED_LAST",
        )
        assert result.what_to_show == "ADJUSTED_LAST"
        call = client._ib.reqHistoricalDataAsync.await_args
        assert call.kwargs["whatToShow"] == "ADJUSTED_LAST"

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

    # ---- IBKR-Reject kommt als errorEvent (NICHT als Exception) -----------
    # Mit RaiseRequestErrors=False loest ib_async das Future leer auf und
    # emittiert den Fehler ueber errorEvent. Der Endpunkt darf dann NICHT
    # still 200 mit leeren records liefern (Silent-Failure).

    async def test_pacing_162_event_maps_to_429(self) -> None:
        client = _make_client(
            bars=[],
            hist_req_id=7,
            hist_error_events=[(7, 162, "Historical Market Data pacing violation")],
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.historical_bars(1, bar_size=BAR_SIZE_DAILY, duration="1 D")
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail["code"] == "ibkr_pacing_violation"
        assert exc_info.value.detail["ibkr_code"] == 162

    async def test_no_security_definition_200_event_maps_to_404(self) -> None:
        client = _make_client(
            bars=[],
            hist_req_id=7,
            hist_error_events=[(7, 200, "No security definition has been found")],
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.historical_bars(1, bar_size=BAR_SIZE_DAILY, duration="1 D")
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "contract_not_found"
        assert exc_info.value.detail["ibkr_code"] == 200

    async def test_market_data_not_subscribed_10162_maps_404_not_429(self) -> None:
        # Regressionsschutz: '162' als Teilstring von 10162 darf NICHT als
        # Pacing (429/Retry) fehlklassifiziert werden - exaktes Code-Mapping.
        client = _make_client(
            bars=[],
            hist_req_id=7,
            hist_error_events=[(7, 10162, "Requested market data is not subscribed")],
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.historical_bars(1, bar_size=BAR_SIZE_DAILY, duration="1 D")
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "historical_data_unavailable"
        assert exc_info.value.detail["ibkr_code"] == 10162

    async def test_benign_event_165_keeps_empty_200(self) -> None:
        # Code 165 (historical data query message) ist kein Fehler - legitim
        # leere Bars bleiben 200 mit leeren records.
        client = _make_client(
            bars=[],
            hist_req_id=7,
            hist_error_events=[(7, 165, "Historical Market Data Service query message")],
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.historical_bars(
            1, bar_size=BAR_SIZE_DAILY, duration="1 D"
        )
        assert result.records == []

    async def test_foreign_reqid_event_ignored(self) -> None:
        # Ein Fehler-Event mit fremder reqId (paralleler Request am geteilten
        # IB-Handle) darf den eigenen Bars-Request nicht scheitern lassen.
        client = _make_client(
            bars=[_make_bar(day=date(2026, 5, 17))],
            hist_req_id=7,
            hist_error_events=[(999, 162, "pacing on another request")],
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.historical_bars(
            1, bar_size=BAR_SIZE_DAILY, duration="1 D"
        )
        assert len(result.records) == 1

    async def test_error_handler_detached_after_bars(self) -> None:
        # Der errorEvent-Handler darf nach dem Request nicht am Handle haengen.
        client = _make_client(bars=[_make_bar(day=date(2026, 5, 17))])
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        before = len(client._ib.errorEvent)
        await service.historical_bars(1, bar_size=BAR_SIZE_DAILY, duration="1 D")
        assert len(client._ib.errorEvent) == before

    async def test_transport_exception_maps_to_502(self) -> None:
        # Echte Transport-/Verbindungs-Exception (kein IBKR-Business-Fehler).
        client = _make_client(bars_raises=ConnectionError("socket closed"))
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.historical_bars(1, bar_size=BAR_SIZE_DAILY, duration="1 D")
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["code"] == "ib_async_error"

    async def test_unqualifiable_conid_none_maps_to_404(self) -> None:
        # ib_async qualifyContractsAsync liefert fuer eine unbekannte/delistete
        # conid [None] (nicht []). _resolve_contract muss daraus 404
        # contract_not_found machen, statt None an den Request-Pfad zu reichen.
        client = _make_client(qualify_result=[None])
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.historical_bars(99, bar_size=BAR_SIZE_DAILY, duration="1 D")
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "contract_not_found"

    async def test_code_162_no_data_message_keeps_empty_200(self) -> None:
        # IBKR ueberlaedt Code 162: neben Pacing auch "HMDS query returned no
        # data" - ein legitim leeres Ergebnis, KEIN Retry-Fall. Dieser Sub-Case
        # muss 200 mit leeren records liefern, nicht 429.
        client = _make_client(
            bars=[],
            hist_req_id=7,
            hist_error_events=[
                (
                    7,
                    162,
                    "Historical Market Data Service error message:HMDS query "
                    "returned no data: ...",
                )
            ],
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.historical_bars(
            1, bar_size=BAR_SIZE_DAILY, duration="1 D"
        )
        assert result.records == []

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
        client = _make_fund_client(
            fundamentals={
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
        client = _make_fund_client(
            fundamentals={
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
        client = _make_fund_client(
            fundamentals={
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

    async def test_partial_records_despite_one_reject(self) -> None:
        # Ein einzelner Report-Reject darf die vorhandenen Daten anderer
        # Reports nicht verwerfen: partielle Ergebnisse bleiben 200.
        client = _make_fund_client(
            fundamentals={
                "ReportSnapshot": (430, "no reuters data for this contract"),
                "RESC": "<resc>data</resc>",
            }
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.fundamentals(
            265598, report_types=["ReportSnapshot", "RESC"]
        )
        assert [r.report_type for r in result.records] == ["RESC"]

    # ---- Reject unterscheidbar von 'kein Report' (Verification) ------------

    async def test_reject_event_is_distinguishable_not_silent_404(self) -> None:
        # Ein IBKR-Reject (kein Report vorhanden) muss vom generischen
        # 'fundamentals_unavailable' unterscheidbar sein: der ibkr_code steht
        # im Detail. Er darf NICHT still als leere records/200 durchgehen.
        client = _make_fund_client(
            fundamentals={"ReportSnapshot": (162, "pacing violation")}
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(265598, report_types=["ReportSnapshot"])
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail["code"] == "ibkr_pacing_violation"
        assert exc_info.value.detail["ibkr_code"] == 162

    async def test_reject_empty_container_not_returned_as_garbage(self) -> None:
        # ib_async loest das Future im Reject-Fall mit einer leeren Liste []
        # auf (kein str). Der Service darf daraus NIEMALS str([])='[]' als
        # Report bauen, sondern muss den Reject erkennen.
        client = _make_fund_client(
            fundamentals={"ReportSnapshot": (10197, "no market data permissions")}
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(265598, report_types=["ReportSnapshot"])
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "historical_data_unavailable"
        assert exc_info.value.detail["ibkr_code"] == 10197

    async def test_reject_foreign_reqid_ignored(self) -> None:
        # Ein Fehler-Event mit fremder reqId (paralleler Request am geteilten
        # IB-Handle) darf einen legitimen Report nicht als Reject verwerfen.
        client = _make_fund_client(
            fundamentals={"ReportSnapshot": "<snapshot>data</snapshot>"},
            foreign_error_event=(999999, 162, "pacing on another request"),
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.fundamentals(265598, report_types=["ReportSnapshot"])
        assert [r.report_type for r in result.records] == ["ReportSnapshot"]

    async def test_error_handler_detached_after_fundamentals(self) -> None:
        client = _make_fund_client(
            fundamentals={"ReportSnapshot": "<snapshot>data</snapshot>"}
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        before = len(client._ib.errorEvent)
        await service.fundamentals(265598, report_types=["ReportSnapshot"])
        assert len(client._ib.errorEvent) == before

    async def test_unqualifiable_conid_none_maps_to_404(self) -> None:
        # Wie beim Historik-Pfad: qualifyContractsAsync liefert [None] fuer eine
        # unbekannte conid. _resolve_contract muss 404 contract_not_found werfen,
        # damit kein None-Contract in den Low-Level-Pfad laeuft (sonst HTTP 500).
        client = _make_fund_client(qualify_result=[None])
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(99, report_types=["ReportSnapshot"])
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "contract_not_found"

    async def test_transport_exception_maps_to_502(self) -> None:
        # Socket-Abbruch waehrend des Fetch: ib_async loest das Future via
        # set_exception(ConnectionError) auf. Der Service muss das - analog
        # Historik/Scanner - auf strukturierten 502 mappen, nicht uncaught als
        # HTTP 500 durchschlagen lassen.
        client = _make_fund_client(
            fundamentals={"ReportSnapshot": ConnectionError("socket disconnect")}
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(265598, report_types=["ReportSnapshot"])
        assert exc_info.value.status_code == 502
        assert exc_info.value.detail["code"] == "ib_async_error"

    async def test_empty_container_without_reject_yields_404(self) -> None:
        # Future loest mit leerem Default-Container [] auf, OHNE Reject-Event.
        # Ohne den isinstance(raw, str)-Guard wuerde [].strip() eine
        # AttributeError (HTTP 500) werfen statt sauber 404.
        client = _make_fund_client(
            fundamentals={"ReportSnapshot": _FUND_EMPTY_CONTAINER}
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(265598, report_types=["ReportSnapshot"])
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "fundamentals_unavailable"

    async def test_benign_own_event_keeps_report(self) -> None:
        # Ein benigner Warning-Code (2104 'market data farm OK') fuer die EIGENE
        # reqId darf einen legitim gelieferten Report NICHT als Reject verwerfen.
        client = _make_fund_client(
            fundamentals={"ReportSnapshot": "<snapshot>data</snapshot>"},
            own_error_events={
                "ReportSnapshot": [(2104, "market data farm connection is OK")]
            },
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        result = await service.fundamentals(265598, report_types=["ReportSnapshot"])
        assert [r.report_type for r in result.records] == ["ReportSnapshot"]

    async def test_first_reject_wins_across_two_rejects(self) -> None:
        # Zwei rejectende Reports ohne Daten: der ERSTE Reject wird durchgereicht
        # (Guard 'first_reject is None'), nicht der letzte.
        client = _make_fund_client(
            fundamentals={
                "ReportSnapshot": (162, "pacing violation"),
                "RESC": (200, "no security definition"),
            }
        )
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(
                265598, report_types=["ReportSnapshot", "RESC"]
            )
        assert exc_info.value.status_code == 429
        assert exc_info.value.detail["code"] == "ibkr_pacing_violation"
        assert exc_info.value.detail["ibkr_code"] == 162

    async def test_empty_report_types_raises_400(self) -> None:
        client = _make_fund_client()
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(265598, report_types=[])
        assert exc_info.value.status_code == 400

    async def test_unknown_report_type_raises_400(self) -> None:
        client = _make_fund_client()
        service = TWSHistoricalService(client, historical_pacing_s=0.0)
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(
                265598, report_types=["ReportSnapshot", "InvalidReport"]
            )
        assert exc_info.value.status_code == 400
        assert "InvalidReport" in exc_info.value.detail["message"]

    async def test_fundamentals_timeout_skips_report(self) -> None:
        # Future wird nie aufgeloest → asyncio.wait_for-Timeout greift; kein
        # Report kam zurueck → 404 wie bei Berechtigungs-Mangel.
        client = _make_fund_client(
            fundamentals={"ReportSnapshot": _FUND_TIMEOUT}
        )
        service = TWSHistoricalService(
            client,
            historical_pacing_s=0.0,
            fundamentals_timeout_s=0.05,
        )
        with pytest.raises(HTTPException) as exc_info:
            await service.fundamentals(265598, report_types=["ReportSnapshot"])
        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["code"] == "fundamentals_unavailable"
        assert client._fund_calls == ["ReportSnapshot"]
