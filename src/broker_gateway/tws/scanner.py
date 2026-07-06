"""IBKR-Marktscanner-Adapter gegen die TWS-Socket-API (RW-07).

Liefert die Daten fuer ``GET /v1/scanner`` und ``GET /v1/scanner/parameters``.
Dieselbe Scanner-Engine wie der IBKR-Desktop-Scanner (``reqScannerSubscription``),
programmatisch nutzbar ueber ``ib_async.IB.reqScannerDataAsync`` — ein
One-Shot-Async-Aufruf, der die ``scannerData``/``scannerDataEnd``-Callbacks
intern kapselt und die Subscription nach dem Ergebnis automatisch cancelt.

Grenzen (IBKR, in der Karte RW-07 dokumentiert):

- **live-only** — der Scanner kennt keinen historischen Lauf; er ersetzt
  keinen Screening-Backtest, sondern baut ein point-in-time-Universe
  *vorwaerts* auf (Forward-Snapshot).
- **max. 50 Treffer je Scan-Code** — ``numberOfRows`` wird gegen
  :data:`MAX_ROWS` validiert (422 statt stiller Kappung), damit ein
  Konsument weiss, dass er nicht mehr bekommt.
- **max. 10 gleichzeitige Scanner-Subscriptions** — eine
  ``asyncio.Semaphore`` (:data:`MAX_CONCURRENT_SCANS`) begrenzt die
  parallel aktiven Scans; ueberzaehlige Aufrufe warten, statt an der
  IBKR-Grenze abzureissen.
- **Scanner liefert nur Kontrakte** — Kurse/Fundamentals werden separat
  ueber ``reqMktData``/``reqFundamentalData`` bzw. die bestehenden
  ``/v1/quotes``- und ``/v1/instruments/{conid}/fundamentals``-Endpunkte
  nachgeladen (siehe docs/api/v1.md).

Marktdaten-Berechtigung:

- Marktdatenbasierte Filter (``abovePrice``/``aboveVolume`` …) und einige
  scanCodes setzen ein Live-Marktdaten-Abo voraus. Fehlt es, liefert IBKR
  weniger oder keine Zeilen bzw. einen Fehler — den mappen wir wie beim
  Historik-Adapter auf einen klaren HTTP-Status mit ``code``-Kennung.

Fundamental-Ratio-Filter (Verification #1 der Karte) laufen **nicht** ueber
die ``ScannerSubscription``-Felder (die decken nur Preis/Volumen/MarketCap/
Ratings), sondern ueber ``scannerSubscriptionFilterOptions`` als Liste von
``TagValue(tag, value)`` — z.B. ``peRatioBelow`` oder ``marketCapAbove1e6``.
Die gueltigen Tags liefert ``GET /v1/scanner/parameters``.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from broker_gateway.tws.client import TWSClient


logger = logging.getLogger(__name__)


# IBKR: der Marktscanner liefert maximal 50 Zeilen je Scan-Code.
MAX_ROWS = 50

# IBKR: maximal 10 gleichzeitig aktive Scanner-Subscriptions pro Session.
MAX_CONCURRENT_SCANS = 10

# Timeout fuer einen einzelnen Scan-Aufruf. reqScannerDataAsync selbst hat
# keinen Timeout; ohne Guard wuerde ein ausbleibendes scannerDataEnd (oder ein
# Fehler-Event mit fremder reqId) den Call unbegrenzt haengen lassen.
SCAN_TIMEOUT_S = 30.0

# Defaults fuer einen typischen US-Aktien-Scan.
DEFAULT_INSTRUMENT = "STK"
DEFAULT_LOCATION_CODE = "STK.US.MAJOR"
DEFAULT_NUMBER_OF_ROWS = MAX_ROWS

# IBKR-Fehlercodes, die fuer einen Scanner-Request KEIN echter Fehler sind:
# ib_async behandelt sie als Warnings (wrapper.error), dazu die 2100-2199
# System-/Connectivity-Meldungen. 165 = "no longer matching results" = der
# Scan ist legitim leer.
_SCANNER_WARNING_CODES: frozenset[int] = frozenset(
    {105, 110, 165, 321, 329, 399, 404, 434, 492, 10167}
)


class ScanRow(BaseModel):
    """Ein Scanner-Treffer — ein Kontrakt plus sein Ranking.

    Quelle: :class:`ib_async.ScanData` (``rank`` + ``contractDetails``).
    Der Scanner liefert bewusst nur Kontrakt-Identitaet, keine Kurse.
    """

    model_config = ConfigDict(frozen=True)

    rank: int = Field(description="0-basierter Rang im Scan (0 = bestes Ergebnis)")
    con_id: int
    symbol: str
    sec_type: str
    exchange: str | None = None
    primary_exchange: str | None = Field(
        default=None,
        description="Heimatboerse — Basis fuer die TICKER.BOERSE-Konvention",
    )
    currency: str | None = None
    distance: str | None = None
    benchmark: str | None = None
    projection: str | None = None
    legs_str: str | None = None


class ScannerResultsResponse(BaseModel):
    """Antwort fuer ``GET /v1/scanner``.

    ``scanned_at`` ist der UTC-Zeitpunkt der Datenbeschaffung — die
    autoritative ``as-of``-Zeit fuer den Forward-Snapshot nach cma-pi-2.
    """

    model_config = ConfigDict(frozen=True)

    scan_code: str
    instrument: str
    location_code: str
    number_of_rows: int = Field(description="Angeforderte Zeilen-Obergrenze (<= 50)")
    scanned_at: datetime
    results: list[ScanRow]


class ScannerParametersResponse(BaseModel):
    """Antwort fuer ``GET /v1/scanner/parameters`` — Roh-XML der gueltigen
    scanCodes, locationCodes und Filter-Tags (Discovery)."""

    model_config = ConfigDict(frozen=True)

    xml: str = Field(description="Roh-XML aus reqScannerParameters (gross)")


class TWSScannerService:
    """Marktscanner-Service auf Basis von ``ib_async``.

    Greift wie ``TWSHistoricalService`` direkt auf den ``_ib``-Handle des
    ``TWSClient`` zu. Eine ``asyncio.Semaphore`` begrenzt die gleichzeitig
    aktiven Scanner-Subscriptions auf die IBKR-Grenze.
    """

    def __init__(
        self,
        client: TWSClient,
        *,
        max_concurrent_scans: int = MAX_CONCURRENT_SCANS,
        scan_timeout_s: float = SCAN_TIMEOUT_S,
    ) -> None:
        self._client = client
        self._max_concurrent_scans = max_concurrent_scans
        self._scan_timeout_s = scan_timeout_s
        self._scan_semaphore = asyncio.Semaphore(max_concurrent_scans)

    # ---- Public API ---------------------------------------------------

    async def scan(
        self,
        *,
        scan_code: str,
        instrument: str = DEFAULT_INSTRUMENT,
        location_code: str = DEFAULT_LOCATION_CODE,
        number_of_rows: int = DEFAULT_NUMBER_OF_ROWS,
        above_price: float | None = None,
        below_price: float | None = None,
        above_volume: int | None = None,
        market_cap_above: float | None = None,
        market_cap_below: float | None = None,
        stock_type_filter: str | None = None,
        filter_options: list[tuple[str, str]] | None = None,
    ) -> ScannerResultsResponse:
        rows = validate_number_of_rows(number_of_rows)
        subscription_cls, tagvalue_cls = self._import_ib_types()

        subscription = subscription_cls(
            numberOfRows=rows,
            instrument=instrument,
            locationCode=location_code,
            scanCode=scan_code,
        )
        if above_price is not None:
            subscription.abovePrice = above_price
        if below_price is not None:
            subscription.belowPrice = below_price
        if above_volume is not None:
            subscription.aboveVolume = above_volume
        if market_cap_above is not None:
            subscription.marketCapAbove = market_cap_above
        if market_cap_below is not None:
            subscription.marketCapBelow = market_cap_below
        if stock_type_filter is not None:
            subscription.stockTypeFilter = stock_type_filter

        filter_tagvalues = [
            tagvalue_cls(tag=tag, value=value)
            for tag, value in (filter_options or [])
        ]

        ib = self._client._ib  # noqa: SLF001 - Low-Level-Bruecke

        # ib_async wirft mit RaiseRequestErrors=False (Projekt-Default) bei
        # IBKR-Request-Fehlern NICHT, sondern loest das Future mit einer leeren
        # Liste auf und emittiert den Fehler ueber errorEvent. Ohne diese
        # Erfassung waere ein Business-Reject (ungueltiger scanCode, Pacing,
        # fehlendes Marktdaten-Abo) nicht von einem legitim leeren Scan zu
        # unterscheiden - beides 200 {results: []} (Silent-Failure).
        collected_errors: list[tuple[int, int, str]] = []

        def _collect_error(
            req_id: int, error_code: int, error_string: str, *_: object
        ) -> None:
            collected_errors.append((req_id, error_code, error_string))

        async with self._scan_semaphore:
            scanned_at = datetime.now(UTC)
            ib.errorEvent += _collect_error
            try:
                data = await asyncio.wait_for(
                    ib.reqScannerDataAsync(
                        subscription,
                        scannerSubscriptionFilterOptions=filter_tagvalues,
                    ),
                    timeout=self._scan_timeout_s,
                )
            except TimeoutError as exc:
                logger.warning(
                    "reqScannerDataAsync(scan_code=%s) Timeout nach %.1fs",
                    scan_code,
                    self._scan_timeout_s,
                )
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail={
                        "code": "scanner_timeout",
                        "message": (
                            f"IBKR-Scan lieferte binnen {self._scan_timeout_s:.0f}s "
                            "kein Ergebnis"
                        ),
                    },
                ) from exc
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "reqScannerDataAsync(scan_code=%s) fehlgeschlagen: %s",
                    scan_code,
                    exc,
                )
                raise _map_ib_error(exc) from exc
            finally:
                ib.errorEvent -= _collect_error

        req_id = getattr(data, "reqId", None)
        fatal = _first_fatal_scanner_error(collected_errors, req_id)
        if fatal is not None:
            error_code, error_string = fatal
            logger.warning(
                "IBKR lehnt Scan ab (scan_code=%s, ibkr_code=%s): %s",
                scan_code,
                error_code,
                error_string,
            )
            raise _map_scanner_error_code(error_code, error_string)

        results = [_map_scan_row(item) for item in (data or [])]
        return ScannerResultsResponse(
            scan_code=scan_code,
            instrument=instrument,
            location_code=location_code,
            number_of_rows=rows,
            scanned_at=scanned_at,
            results=results,
        )

    async def scanner_parameters(self) -> ScannerParametersResponse:
        ib = self._client._ib  # noqa: SLF001
        try:
            xml = await ib.reqScannerParametersAsync()
        except Exception as exc:  # noqa: BLE001
            logger.warning("reqScannerParametersAsync fehlgeschlagen: %s", exc)
            raise _map_ib_error(exc) from exc
        return ScannerParametersResponse(xml=str(xml or ""))

    # ---- Internals ----------------------------------------------------

    def _import_ib_types(self) -> tuple[Any, Any]:
        try:
            from ib_async.contract import TagValue  # type: ignore[import-untyped]
            from ib_async.objects import (  # type: ignore[import-untyped]
                ScannerSubscription,
            )
        except ImportError:  # pragma: no cover
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "ib_async_missing",
                    "message": "ib_async ist nicht installiert",
                },
            )
        return ScannerSubscription, TagValue


def _map_scan_row(data: Any) -> ScanRow:
    """Mappt eine ib_async-``ScanData`` auf :class:`ScanRow`."""
    contract = data.contractDetails.contract
    return ScanRow(
        rank=int(data.rank),
        con_id=int(getattr(contract, "conId", 0) or 0),
        symbol=getattr(contract, "symbol", "") or "",
        sec_type=getattr(contract, "secType", "") or "",
        exchange=getattr(contract, "exchange", "") or None,
        primary_exchange=getattr(contract, "primaryExchange", "") or None,
        currency=getattr(contract, "currency", "") or None,
        distance=getattr(data, "distance", "") or None,
        benchmark=getattr(data, "benchmark", "") or None,
        projection=getattr(data, "projection", "") or None,
        legs_str=getattr(data, "legsStr", "") or None,
    )


def _is_benign_scanner_code(code: int) -> bool:
    """True fuer IBKR-Codes, die fuer einen Scan-Request kein Fehler sind."""
    return code in _SCANNER_WARNING_CODES or 2100 <= code < 2200


def _first_fatal_scanner_error(
    errors: list[tuple[int, int, str]], req_id: int | None
) -> tuple[int, str] | None:
    """Erster fataler Fehler fuer die eigene reqId, sonst None.

    Fehler-Events fremder reqIds (parallele Requests am geteilten IB-Handle)
    und benigne Codes (165 no-results, 2100-2199 System-Meldungen) werden
    ignoriert.
    """
    for event_req_id, code, message in errors:
        if req_id is not None and event_req_id != req_id:
            continue
        if _is_benign_scanner_code(code):
            continue
        return code, message
    return None


def _map_scanner_error_code(code: int, message: str) -> HTTPException:
    """Mappt einen IBKR-Scanner-Fehlercode auf HTTPException.

    Exakter Code-Vergleich, kein Substring: Error 162 (Pacing) -> 429. Der
    Code 10162 ('market data not subscribed') enthaelt den Teilstring '162',
    ist aber ein dauerhafter Berechtigungsfehler und darf NICHT als Retry
    (429) erscheinen -> 422. Der echte IBKR-Code steht als ``ibkr_code`` im
    Detail, damit der Konsument die Ursache kennt.
    """
    if code == 162:
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "ibkr_pacing_violation",
                "message": "IBKR-Pacing-Limit erreicht (Error 162). Spaeter erneut.",
                "ibkr_code": 162,
            },
        )
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "invalid_scan_request",
            "message": f"IBKR lehnt die Scan-Anfrage ab (Code {code}): {message}",
            "ibkr_code": code,
        },
    )


def _map_ib_error(exc: Exception) -> HTTPException:
    """Mappt eine ib_async-Exception auf HTTPException.

    Der IBKR-Business-Fehlerpfad laeuft ueber errorEvent + _map_scanner_error_code
    (ib_async wirft mit RaiseRequestErrors=False nicht). Diese Funktion greift
    nur fuer echte Exceptions: eine ``RequestError`` (falls RaiseRequestErrors
    doch aktiviert ist) traegt ein ``.code`` und wird ueber ihren exakten Code
    gemappt; alles andere (ConnectionError, Socket-Fehler) auf 502.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return _map_scanner_error_code(code, str(exc))
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": "ib_async_error", "message": str(exc)},
    )


def validate_number_of_rows(value: int) -> int:
    """Validiert ``number_of_rows`` gegen die IBKR-50-Treffer-Grenze.

    Gibt den Wert unveraendert zurueck, wenn er in ``1..MAX_ROWS`` liegt,
    sonst HTTP 422 mit ``code=invalid_number_of_rows``.
    """
    if value < 1 or value > MAX_ROWS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_number_of_rows",
                "message": (
                    f"number_of_rows={value} ausserhalb 1..{MAX_ROWS} "
                    f"(IBKR liefert maximal {MAX_ROWS} Treffer je Scan)"
                ),
            },
        )
    return value


def parse_filter_options(raw: list[str] | None) -> list[tuple[str, str]]:
    """Parsing fuer den ``?filter=tag:value``-Query-Param (wiederholbar).

    Jedes Element hat die Form ``<tag>:<value>``; nur am **ersten**
    Doppelpunkt gesplittet, damit Werte selbst ``:`` enthalten duerfen.
    Whitespace um tag/value wird getrimmt. ``None``/leer → ``[]``.
    Fehlender Doppelpunkt, leerer tag oder leerer value → HTTP 422 mit
    ``code=invalid_filter_option``.
    """
    if not raw:
        return []
    parsed: list[tuple[str, str]] = []
    for item in raw:
        if ":" not in item:
            raise _invalid_filter_option(item)
        tag, value = item.split(":", 1)
        tag = tag.strip()
        value = value.strip()
        if not tag or not value:
            raise _invalid_filter_option(item)
        parsed.append((tag, value))
    return parsed


def _invalid_filter_option(item: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "invalid_filter_option",
            "message": (
                f"filter={item!r} nicht im Format 'tag:value' "
                "(tag und value duerfen nicht leer sein)"
            ),
        },
    )
