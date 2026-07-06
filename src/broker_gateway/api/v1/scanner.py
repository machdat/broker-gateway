"""GET /v1/scanner - IBKR-Marktscanner (Live-Screening, RW-07).

Programmatischer Zugriff auf dieselbe Scanner-Engine wie der IBKR-Desktop-
Scanner (``reqScannerSubscription``). Live-only: baut ein point-in-time-
Universe vorwaerts auf (Forward-Snapshot nach cma-pi-2, Folgekarte).

Zwei Endpunkte:

- ``GET /v1/scanner`` - fuehrt einen Scan aus und liefert die Kandidaten
  (Kontrakte + Rang). Fundamental-Ratio-Filter ueber wiederholbare
  ``?filter=tag:value``-Params (``scannerSubscriptionFilterOptions``).
- ``GET /v1/scanner/parameters`` - Roh-XML der gueltigen scanCodes,
  locationCodes und Filter-Tags (Discovery fuer den Konsumenten).

Im cp-Backend liefert der Default-Provider 503 - der Scanner laeuft
ausschliesslich ueber die TWS-Socket-API (ib_async).
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from broker_gateway.auth.middleware import require_scope
from broker_gateway.auth.models import SCOPE_SCANNER_READ, Token
from broker_gateway.cp.lifecycle import AuthLifecycle, require_session_ok
from broker_gateway.tws.scanner import (
    DEFAULT_INSTRUMENT,
    DEFAULT_LOCATION_CODE,
    DEFAULT_NUMBER_OF_ROWS,
    ScannerParametersResponse,
    ScannerResultsResponse,
    TWSScannerService,
    parse_filter_options,
    validate_number_of_rows,
)


router = APIRouter(prefix="/scanner", tags=["scanner"])


def get_scanner_service() -> TWSScannerService:
    """Dependency fuer den Marktscanner.

    Wird nur im TWS-Backend (BG_BACKEND=tws) via dependency_overrides
    verdrahtet. Im cp-Backend liefert dieser Default eine 503 mit klarer
    code-Kennung - der Scanner laeuft ausschliesslich ueber die TWS-
    Socket-API (ib_async).
    """
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "service_unavailable_in_cp_mode",
            "message": "scanner nur im TWS-Backend (BG_BACKEND=tws) verfuegbar",
        },
    )


@router.get(
    "",
    response_model=ScannerResultsResponse,
    summary="IBKR-Marktscanner ausfuehren (Live-Screening)",
)
async def run_scan(
    _scope: Annotated[Token, Depends(require_scope(SCOPE_SCANNER_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[TWSScannerService, Depends(get_scanner_service)],
    scan_code: Annotated[
        str,
        Query(
            min_length=1,
            description=(
                "IBKR-scanCode, z.B. TOP_PERC_GAIN, MOST_ACTIVE, "
                "HIGH_DIVIDEND_YIELD, HOT_BY_VOLUME. Gueltige Codes: "
                "GET /v1/scanner/parameters."
            ),
        ),
    ],
    instrument: Annotated[
        str, Query(description="IBKR-Instrument, z.B. STK")
    ] = DEFAULT_INSTRUMENT,
    location_code: Annotated[
        str,
        Query(description="IBKR-locationCode, z.B. STK.US.MAJOR, STK.EU, STK.HK.MAJOR"),
    ] = DEFAULT_LOCATION_CODE,
    number_of_rows: Annotated[
        int,
        Query(description="Zeilen-Obergrenze (1..50; IBKR liefert max. 50)"),
    ] = DEFAULT_NUMBER_OF_ROWS,
    above_price: Annotated[
        float | None, Query(description="Untergrenze Kurs (Marktdaten-Filter)")
    ] = None,
    below_price: Annotated[
        float | None, Query(description="Obergrenze Kurs (Marktdaten-Filter)")
    ] = None,
    above_volume: Annotated[
        int | None, Query(description="Untergrenze Tagesvolumen (Marktdaten-Filter)")
    ] = None,
    market_cap_above: Annotated[
        float | None, Query(description="Untergrenze Marktkapitalisierung")
    ] = None,
    market_cap_below: Annotated[
        float | None, Query(description="Obergrenze Marktkapitalisierung")
    ] = None,
    stock_type_filter: Annotated[
        str | None, Query(description="z.B. CORP, ADR, ETF, ALL")
    ] = None,
    filter: Annotated[  # noqa: A002 - externes API-Naming
        list[str] | None,
        Query(
            description=(
                "Wiederholbarer Fundamental-Ratio-Filter im Format tag:value, "
                "z.B. filter=peRatioBelow:20&filter=marketCapAbove1e6:500. "
                "Gueltige Tags: GET /v1/scanner/parameters."
            ),
        ),
    ] = None,
) -> ScannerResultsResponse:
    rows = validate_number_of_rows(number_of_rows)
    filter_options = parse_filter_options(filter)
    return await service.scan(
        scan_code=scan_code,
        instrument=instrument,
        location_code=location_code,
        number_of_rows=rows,
        above_price=above_price,
        below_price=below_price,
        above_volume=above_volume,
        market_cap_above=market_cap_above,
        market_cap_below=market_cap_below,
        stock_type_filter=stock_type_filter,
        filter_options=filter_options,
    )


@router.get(
    "/parameters",
    response_model=ScannerParametersResponse,
    summary="Gueltige scanCodes/locationCodes/Filter-Tags (Roh-XML)",
)
async def scanner_parameters(
    _scope: Annotated[Token, Depends(require_scope(SCOPE_SCANNER_READ))],
    _session: Annotated[AuthLifecycle, Depends(require_session_ok)],
    service: Annotated[TWSScannerService, Depends(get_scanner_service)],
) -> ScannerParametersResponse:
    return await service.scanner_parameters()
