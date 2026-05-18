"""GET /v1/instruments/* — Search, Detail, Historical Bars, Fundamentals."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import (
    SCOPE_FUNDAMENTALS_READ,
    SCOPE_HISTORICAL_READ,
    SCOPE_INSTRUMENTS_READ,
    Token,
)
from broker_gateway.cp.instruments import (
    Instrument,
    InstrumentDetail,
    InstrumentsService,
)
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok
from broker_gateway.tws.historical import (
    BAR_SIZE_15MIN,
    BAR_SIZE_1MIN,
    BAR_SIZE_DAILY,
    BAR_SIZE_HOURLY,
    DEFAULT_DURATION_15MIN,
    DEFAULT_DURATION_1MIN,
    DEFAULT_DURATION_DAILY,
    DEFAULT_DURATION_HOURLY,
    FundamentalsResponse,
    HistoricalBarsResponse,
    TWSHistoricalService,
    parse_report_types,
)


router = APIRouter(prefix="/instruments", tags=["instruments"])


def get_instruments_service() -> InstrumentsService:
    raise RuntimeError(
        "get_instruments_service muss in der App per dependency_overrides gesetzt werden"
    )


def get_historical_service() -> TWSHistoricalService:
    """Dependency fuer historische Bars + Fundamentals.

    Wird nur im TWS-Backend (BG_BACKEND=tws) via dependency_overrides
    verdrahtet. Im cp-Backend liefert dieser Default eine 503 mit klarer
    code-Kennung — historische Bars und Fundamentals laufen ausschliesslich
    ueber die TWS-Socket-API (ib_async).
    """
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "service_unavailable_in_cp_mode",
            "message": (
                "historical/fundamentals nur im TWS-Backend "
                "(BG_BACKEND=tws) verfuegbar"
            ),
        },
    )


@router.get(
    "/search",
    response_model=list[Instrument],
    summary="Symbol- oder ISIN-Lookup (TTL-Cache 7 Tage)",
)
async def search_instruments(
    _scope: Annotated[Token, Depends(require_scope(SCOPE_INSTRUMENTS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[InstrumentsService, Depends(get_instruments_service)],
    symbol: Annotated[
        str | None, Query(min_length=1, description="z.B. AAPL")
    ] = None,
    exchange: Annotated[
        str | None, Query(description="z.B. NASDAQ - nur in Kombination mit symbol")
    ] = None,
    isin: Annotated[
        str | None,
        Query(
            min_length=12,
            max_length=12,
            description="ISO 6166, z.B. DE0007164600 (SAP)",
        ),
    ] = None,
    mic: Annotated[
        str | None,
        Query(
            description=(
                "ISO 10383 MIC zur Cross-Listing-Disambiguation, "
                "z.B. XETR oder XNYS - nur in Kombination mit isin"
            ),
        ),
    ] = None,
) -> list[Instrument]:
    if symbol is None and isin is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="symbol oder isin muss angegeben werden",
        )
    if symbol is not None and isin is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="symbol und isin schliessen sich aus - genau einer der beiden",
        )
    if isin is not None:
        if exchange is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="exchange ist nur in Kombination mit symbol erlaubt",
            )
        return await service.search_by_isin(isin, mic)
    if mic is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="mic ist nur in Kombination mit isin erlaubt",
        )
    assert symbol is not None  # narrowing
    return await service.search(symbol, exchange)


@router.get(
    "/{conid}",
    response_model=InstrumentDetail,
    summary="Instrument-Detail per conid",
)
async def get_instrument(
    conid: int,
    _scope: Annotated[Token, Depends(require_scope(SCOPE_INSTRUMENTS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[InstrumentsService, Depends(get_instruments_service)],
) -> InstrumentDetail:
    return await service.info(conid)


# ---------------------------------------------------------------------------
# Historical Bars (Karte a5c7ff1c)
#
# Vier Endpoints mit identischem Vertrag, nur Bar-Size + Default-Duration
# unterscheiden sich. IBKR-Pacing (60 Requests / 10 min) wird im Service
# enforced. useRTH defaultet auf True — wer Pre-/Post-Market braucht,
# setzt es explizit auf False.
# ---------------------------------------------------------------------------


async def _historical_endpoint(
    *,
    conid: int,
    bar_size: str,
    duration: str,
    use_rth: bool,
    service: TWSHistoricalService,
) -> HistoricalBarsResponse:
    return await service.historical_bars(
        conid,
        bar_size=bar_size,
        duration=duration,
        use_rth=use_rth,
    )


@router.get(
    "/{conid}/historical/daily",
    response_model=HistoricalBarsResponse,
    summary="Tages-Bars (EOD)",
)
async def get_historical_daily(
    conid: int,
    _scope: Annotated[Token, Depends(require_scope(SCOPE_HISTORICAL_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[TWSHistoricalService, Depends(get_historical_service)],
    duration: Annotated[
        str,
        Query(
            description="IBKR-Duration-String, z.B. '1 Y', '6 M', '90 D'",
        ),
    ] = DEFAULT_DURATION_DAILY,
    useRTH: Annotated[  # noqa: N803 — externes API-Naming
        bool,
        Query(description="Regular Trading Hours only (Default True)"),
    ] = True,
) -> HistoricalBarsResponse:
    return await _historical_endpoint(
        conid=conid,
        bar_size=BAR_SIZE_DAILY,
        duration=duration,
        use_rth=useRTH,
        service=service,
    )


@router.get(
    "/{conid}/historical/hourly",
    response_model=HistoricalBarsResponse,
    summary="1-Stunden-Bars",
)
async def get_historical_hourly(
    conid: int,
    _scope: Annotated[Token, Depends(require_scope(SCOPE_HISTORICAL_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[TWSHistoricalService, Depends(get_historical_service)],
    duration: Annotated[
        str,
        Query(
            description="IBKR-Duration-String, z.B. '30 D', '60 D', '90 D'",
        ),
    ] = DEFAULT_DURATION_HOURLY,
    useRTH: Annotated[  # noqa: N803
        bool, Query(description="Regular Trading Hours only (Default True)")
    ] = True,
) -> HistoricalBarsResponse:
    return await _historical_endpoint(
        conid=conid,
        bar_size=BAR_SIZE_HOURLY,
        duration=duration,
        use_rth=useRTH,
        service=service,
    )


@router.get(
    "/{conid}/historical/15min",
    response_model=HistoricalBarsResponse,
    summary="15-Minuten-Bars",
)
async def get_historical_15min(
    conid: int,
    _scope: Annotated[Token, Depends(require_scope(SCOPE_HISTORICAL_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[TWSHistoricalService, Depends(get_historical_service)],
    duration: Annotated[
        str,
        Query(description="IBKR-Duration-String, z.B. '7 D', '14 D'"),
    ] = DEFAULT_DURATION_15MIN,
    useRTH: Annotated[  # noqa: N803
        bool, Query(description="Regular Trading Hours only (Default True)")
    ] = True,
) -> HistoricalBarsResponse:
    return await _historical_endpoint(
        conid=conid,
        bar_size=BAR_SIZE_15MIN,
        duration=duration,
        use_rth=useRTH,
        service=service,
    )


@router.get(
    "/{conid}/historical/1min",
    response_model=HistoricalBarsResponse,
    summary="1-Minuten-Bars",
)
async def get_historical_1min(
    conid: int,
    _scope: Annotated[Token, Depends(require_scope(SCOPE_HISTORICAL_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[TWSHistoricalService, Depends(get_historical_service)],
    duration: Annotated[
        str,
        Query(description="IBKR-Duration-String, z.B. '1 D', '2 D'"),
    ] = DEFAULT_DURATION_1MIN,
    useRTH: Annotated[  # noqa: N803
        bool, Query(description="Regular Trading Hours only (Default True)")
    ] = True,
) -> HistoricalBarsResponse:
    return await _historical_endpoint(
        conid=conid,
        bar_size=BAR_SIZE_1MIN,
        duration=duration,
        use_rth=useRTH,
        service=service,
    )


# ---------------------------------------------------------------------------
# Fundamentals (Karte a5c7ff1c)
# ---------------------------------------------------------------------------


@router.get(
    "/{conid}/fundamentals",
    response_model=FundamentalsResponse,
    summary="Reuters-Fundamentals (Snapshot, FinSummary, FinStatements, RESC)",
)
async def get_fundamentals(
    conid: int,
    _scope: Annotated[Token, Depends(require_scope(SCOPE_FUNDAMENTALS_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[TWSHistoricalService, Depends(get_historical_service)],
    report_types: Annotated[
        str | None,
        Query(
            description=(
                "Komma-separierte Liste der Reuters-Report-Typen. Default: "
                "ReportSnapshot,ReportsFinSummary,ReportsFinStatements,RESC. "
                "Erlaubt zusaetzlich: ReportsOwnership, CalendarReport."
            ),
        ),
    ] = None,
) -> FundamentalsResponse:
    types = parse_report_types(report_types)
    return await service.fundamentals(conid, report_types=types)
