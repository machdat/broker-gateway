"""Historical-Bars- und Fundamentals-Adapter gegen die TWS-Socket-API.

Liefert die Daten fuer ``GET /v1/instruments/{conid}/historical/*`` und
``GET /v1/instruments/{conid}/fundamentals`` (Karte ``a5c7ff1c``).

Pacing-Disziplin:

- IBKR limitiert ``reqHistoricalData`` auf 60 Requests pro 10 Minuten.
  Wir serialisieren Calls ueber einen ``asyncio.Lock`` und erzwingen
  einen Mindestabstand von 10.5 Sekunden zwischen aufeinanderfolgenden
  Aufrufen. Das ergibt ~57 Calls pro 10 Minuten — sicher unter dem
  Limit. Bei Burst-Bedarf kann eine sliding-window-Logik in einer
  Folgekarte ergaenzt werden.
- ``reqFundamentalData`` hat kein IBKR-seitiges Pacing-Limit, braucht
  aber pro Report-Typ bis zu 20 Sekunden, weil Reuters den XML-Blob
  asynchron baut. Wir setzen ein per-Report-Timeout von 20 Sekunden
  und sammeln Ergebnisse pro Report-Typ.

Reuters-/Marktdaten-Berechtigung:

- Auf dem Paper-Account fehlen u.a. Reuters-Fundamentals und EU-RT-
  Marktdaten. ib_async wirft in dem Fall ``RequestError`` oder liefert
  einen leeren Bar-Stream. Wir mappen Berechtigungs-Mangel auf HTTP 404
  mit klarer ``code``-Kennung, damit Konsumenten zwischen "Symbol
  unbekannt" und "Daten nicht abonniert" unterscheiden koennen.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, NamedTuple

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from broker_gateway.tws.ib_errors import (
    collect_request_errors,
    first_fatal_ib_error,
)
from broker_gateway.tws.types import Bar

if TYPE_CHECKING:
    from broker_gateway.tws.client import TWSClient


logger = logging.getLogger(__name__)


# IBKR: 60 historische Requests / 10 Minuten. 10.5 s pro Slot puffert
# Round-Trip-Jitter und liefert ~57 / 10 min — sicher unter dem Limit.
_HISTORICAL_PACING_S = 10.5

# IBKR-Empfehlung: 20 s pro Reuters-Report. Bei Timeout liefern wir den
# Report leer (kein 504), damit ein einzelner Reuters-Hicks die uebrigen
# Reports nicht killt.
_FUNDAMENTALS_TIMEOUT_S = 20.0


DEFAULT_FUNDAMENTAL_REPORTS: tuple[str, ...] = (
    "ReportSnapshot",
    "ReportsFinSummary",
    "ReportsFinStatements",
    "RESC",
)


# Whitelist der akzeptierten Reuters-Report-Typen. Aus IBKR-Doku +
# ib_async-Konstanten. Karten-Wunsch deckt die ueblichen Snapshot/
# Financials-Berichte ab.
ALLOWED_FUNDAMENTAL_REPORTS: frozenset[str] = frozenset(
    {
        "ReportSnapshot",
        "ReportsFinSummary",
        "ReportsFinStatements",
        "ReportsOwnership",
        "RESC",
        "CalendarReport",
    }
)


# Default-whatToShow. TRADES = ungeadjustierte (split-adjustierte, aber
# NICHT dividend-adjustierte) Trade-Bars. Rueckwaertskompatibler Default,
# damit bestehende Consumer ohne Param unveraendert bedient werden.
DEFAULT_WHAT_TO_SHOW = "TRADES"


# Whitelist der akzeptierten whatToShow-Werte. ADJUSTED_LAST liefert
# split- UND dividend-adjustierte Tagesbars (Anforderung algotrade-
# backtest, Karte 6c1da48e). MIDPOINT/BID/ASK fuer FX- und Quote-Bars.
# IBKR unterstuetzt weitere Werte (BID_ASK, HISTORICAL_VOLATILITY, ...),
# die hier bewusst nicht freigeschaltet sind.
ALLOWED_WHAT_TO_SHOW: frozenset[str] = frozenset(
    {
        "TRADES",
        "ADJUSTED_LAST",
        "MIDPOINT",
        "BID",
        "ASK",
    }
)


# bar-size-Mapping fuer die vier Endpoints. ib_async erwartet die
# IBKR-Strings, nicht unsere internen Aliase.
BAR_SIZE_DAILY = "1 day"
BAR_SIZE_HOURLY = "1 hour"
BAR_SIZE_15MIN = "15 mins"
BAR_SIZE_1MIN = "1 min"


# Duration-Defaults pro Endpoint. IBKR-Doc: "1 Y" = 1 Jahr, "30 D" = 30
# Tage, "7 D" = 7 Tage, "1 D" = 1 Tag. Karten-Default-Werte.
DEFAULT_DURATION_DAILY = "1 Y"
DEFAULT_DURATION_HOURLY = "30 D"
DEFAULT_DURATION_15MIN = "7 D"
DEFAULT_DURATION_1MIN = "1 D"


class HistoricalBarsResponse(BaseModel):
    """Antwort fuer ``GET /v1/instruments/{conid}/historical/*``."""

    model_config = ConfigDict(frozen=True)

    conid: int
    bar_size: str = Field(description="IBKR-Bar-Size-String, z.B. '1 day'")
    duration: str = Field(description="IBKR-Duration-String, z.B. '1 Y'")
    use_rth: bool
    what_to_show: str = Field(
        description="Effektiv genutztes IBKR-whatToShow, z.B. 'TRADES', 'ADJUSTED_LAST'"
    )
    records: list[Bar]


class FundamentalReport(BaseModel):
    """Ein einzelner Reuters-Report."""

    model_config = ConfigDict(frozen=True)

    report_type: str
    xml: str = Field(description="Roh-XML von Reuters (kann leer sein)")


class FundamentalsResponse(BaseModel):
    """Antwort fuer ``GET /v1/instruments/{conid}/fundamentals``."""

    model_config = ConfigDict(frozen=True)

    conid: int
    records: list[FundamentalReport]


class _FundamentalResult(NamedTuple):
    """Ergebnis eines einzelnen Reuters-Report-Fetch.

    Genau eines von ``xml`` / ``reject`` ist gesetzt, oder beide sind None:
      - ``xml`` gesetzt    -> Report mit Daten
      - ``reject`` gesetzt -> IBKR-Reject ``(code, message)`` fuer diese reqId
      - beide None         -> legitim kein Report (leer / Timeout)
    """

    xml: str | None
    reject: tuple[int, str] | None


class TWSHistoricalService:
    """Bars- und Fundamentals-Service auf Basis von ``ib_async``.

    Greift wie ``TWSInstrumentsService`` direkt auf den ``_ib``-Handle
    des ``TWSClient``-Singletons zu. Eigener Lock + Last-Timestamp fuer
    historische Bars, damit das IBKR-Pacing eingehalten wird, auch wenn
    parallele Requests reinkommen.
    """

    def __init__(
        self,
        client: TWSClient,
        *,
        historical_pacing_s: float = _HISTORICAL_PACING_S,
        fundamentals_timeout_s: float = _FUNDAMENTALS_TIMEOUT_S,
    ) -> None:
        self._client = client
        self._historical_pacing_s = historical_pacing_s
        self._fundamentals_timeout_s = fundamentals_timeout_s
        self._historical_lock = asyncio.Lock()
        self._last_historical_at: float = 0.0

    # ---- Public API ---------------------------------------------------

    async def historical_bars(
        self,
        conid: int,
        *,
        bar_size: str,
        duration: str,
        use_rth: bool = True,
        what_to_show: str = DEFAULT_WHAT_TO_SHOW,
    ) -> HistoricalBarsResponse:
        contract = await self._resolve_contract(conid)
        bars_raw = await self._reqHistoricalData(
            contract,
            duration=duration,
            bar_size=bar_size,
            what_to_show=what_to_show,
            use_rth=use_rth,
        )
        records = [Bar.from_bar_data(b) for b in bars_raw]
        return HistoricalBarsResponse(
            conid=conid,
            bar_size=bar_size,
            duration=duration,
            use_rth=use_rth,
            what_to_show=what_to_show,
            records=records,
        )

    async def fundamentals(
        self,
        conid: int,
        *,
        report_types: list[str],
    ) -> FundamentalsResponse:
        if not report_types:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_input", "message": "report_types darf nicht leer sein"},
            )
        unknown = sorted(set(report_types) - ALLOWED_FUNDAMENTAL_REPORTS)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "invalid_input",
                    "message": f"unbekannte report_types: {unknown}",
                },
            )

        contract = await self._resolve_contract(conid)
        records: list[FundamentalReport] = []
        first_reject: tuple[int, str] | None = None
        for report_type in report_types:
            result = await self._fetch_one_fundamental(contract, report_type)
            if result.xml is not None:
                records.append(
                    FundamentalReport(report_type=report_type, xml=result.xml)
                )
            elif result.reject is not None and first_reject is None:
                first_reject = result.reject

        if not records:
            if first_reject is not None:
                # Ein echter IBKR-Reject (Pacing, fehlende Berechtigung) - vom
                # generischen 'kein Report' durch den ibkr_code unterscheidbar.
                error_code, error_string = first_reject
                logger.warning(
                    "IBKR lehnt Fundamentals-Request ab (conid=%s, ibkr_code=%s): %s",
                    conid,
                    error_code,
                    error_string,
                )
                raise _map_historical_error_code(error_code, error_string)
            # Kein einziger Report kam zurueck, aber auch kein Reject →
            # Reuters-Coverage fehlt fuer dieses Symbol (legitim leer).
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "fundamentals_unavailable",
                    "message": (
                        "Reuters-Fundamentals nicht verfuegbar fuer diesen "
                        "Account oder dieses Symbol"
                    ),
                },
            )
        return FundamentalsResponse(conid=conid, records=records)

    # ---- Internals ----------------------------------------------------

    async def _resolve_contract(self, conid: int) -> Any:
        """Aufloesung conid → qualifizierter Contract via ib_async."""
        contract_class = await self._import_contract_class()
        probe = contract_class(conId=conid, exchange="SMART")
        ib = self._client._ib  # noqa: SLF001 - Low-Level-Bruecke
        try:
            results = await ib.qualifyContractsAsync(probe)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "qualifyContractsAsync(conid=%s) fehlgeschlagen: %s", conid, exc
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "ib_async_error",
                    "message": f"qualifyContractsAsync fehlgeschlagen: {exc}",
                },
            ) from exc
        if not results:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "contract_not_found",
                    "message": f"conid {conid} unbekannt",
                },
            )
        # ib_async legt fuer eine unbekannte/delistete conid ein None in den
        # Ergebnis-Slot (RaiseRequestErrors=False: IBKR-Error 200 loest das
        # contractDetails-Future leer auf -> qualifyContractsAsync haengt None
        # an). results ist bei einer Einzel-Probe immer laenge 1, also greift
        # das obige `if not results` nicht - der None-Slot muss hier abgefangen
        # werden, sonst laeuft ein None-Contract in den Request-Pfad (Historik
        # -> 502, Fundamentals -> HTTP 500).
        first = results[0]
        if isinstance(first, list):
            first = first[0] if first else None
        if first is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "contract_not_found",
                    "message": f"conid {conid} unbekannt",
                },
            )
        return first

    async def _reqHistoricalData(
        self,
        contract: Any,
        *,
        duration: str,
        bar_size: str,
        what_to_show: str,
        use_rth: bool,
    ) -> list[Any]:
        ib = self._client._ib  # noqa: SLF001
        # ib_async wirft mit RaiseRequestErrors=False (Projekt-Default) bei
        # IBKR-Request-Fehlern NICHT, sondern loest das Future mit einer leeren
        # BarDataList auf und emittiert den Fehler ueber errorEvent. Ohne diese
        # Erfassung waere ein Business-Reject (Pacing 162, 200 no security def,
        # 10162 market data not subscribed) nicht von legitim leeren Bars zu
        # unterscheiden - beides 200 mit leeren records (Silent-Failure). Der
        # gemeinsame Helper (auch vom Scanner genutzt) sammelt die Events und
        # filtert nach der eigenen reqId aus der BarDataList.
        async with self._historical_lock:
            wait = self._historical_pacing_s - (
                time.monotonic() - self._last_historical_at
            )
            if wait > 0:
                await asyncio.sleep(wait)
            with collect_request_errors(ib) as collected_errors:
                try:
                    bars = await ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime="",
                        durationStr=duration,
                        barSizeSetting=bar_size,
                        whatToShow=what_to_show,
                        useRTH=use_rth,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "reqHistoricalDataAsync(conid=%s, bar_size=%s) "
                        "fehlgeschlagen: %s",
                        getattr(contract, "conId", "?"),
                        bar_size,
                        exc,
                    )
                    raise _map_ib_error(exc) from exc
                finally:
                    self._last_historical_at = time.monotonic()

        req_id = getattr(bars, "reqId", None)
        fatal = first_fatal_ib_error(collected_errors, req_id)
        if fatal is not None:
            error_code, error_string = fatal
            # IBKR-Code 162 ist ueberladen: neben der Pacing-Violation traegt er
            # auch "HMDS query returned no data" - ein legitim leeres Ergebnis,
            # KEIN Retry-Fall. Diesen eng umrissenen Sub-Case (nur bei exaktem
            # Code 162 + no-data-Message; kein Rueckfall zum 10162-Teilstring-
            # Bug) als leere 200 durchreichen statt als 429.
            if error_code == 162 and "query returned no data" in error_string.lower():
                logger.info(
                    "IBKR-162 no-data (conid=%s): legitim leeres Ergebnis",
                    getattr(contract, "conId", "?"),
                )
                return list(bars or [])
            logger.warning(
                "IBKR lehnt Historik-Request ab (conid=%s, ibkr_code=%s): %s",
                getattr(contract, "conId", "?"),
                error_code,
                error_string,
            )
            raise _map_historical_error_code(error_code, error_string)
        return list(bars or [])

    async def _fetch_one_fundamental(
        self, contract: Any, report_type: str
    ) -> _FundamentalResult:
        """Holt einen Reuters-Report ueber den ib_async-Low-Level-Pfad.

        ``reqFundamentalDataAsync`` liefert nur einen ``str`` ohne reqId; fuer
        die errorEvent-Zuordnung (Reject vs. 'kein Report') brauchen wir aber
        die reqId. Deshalb allozieren wir sie selbst (``client.getReqId``) und
        starten den Request ueber ``wrapper.startReq`` +
        ``client.reqFundamentalData`` — exakt das, was
        ``reqFundamentalDataAsync`` intern tut, nur mit sichtbarer reqId. So
        laesst sich ein IBKR-Reject fuer die eigene reqId von einem legitim
        leeren Report unterscheiden, ohne Cross-Talk mit parallelen Requests
        am geteilten IB-Handle.

        Siehe :class:`_FundamentalResult` fuer die Rueckgabe-Semantik.
        """
        ib = self._client._ib  # noqa: SLF001
        # Der synchrone Request-Aufbau (getReqId/reqFundamentalData) und das
        # await werden von einem generischen Handler umschlossen: ib_async loest
        # bei einem Socket-Abbruch das Future via set_exception(ConnectionError)
        # auf bzw. getReqId wirft, wenn nicht verbunden. Analog Historik/Scanner
        # mappen wir das auf einen strukturierten 502, statt es uncaught als
        # HTTP 500 durchschlagen zu lassen. Der innere TimeoutError-Zweig
        # returniert vorher (die "leerer Report bei Timeout"-Semantik bleibt).
        try:
            req_id = ib.client.getReqId()
            with collect_request_errors(ib) as collected_errors:
                future = ib.wrapper.startReq(req_id, contract)
                ib.client.reqFundamentalData(req_id, contract, report_type, [])
                try:
                    raw = await asyncio.wait_for(
                        future, timeout=self._fundamentals_timeout_s
                    )
                except TimeoutError:
                    logger.warning(
                        "reqFundamentalData(conid=%s, report=%s) Timeout nach %.1fs",
                        getattr(contract, "conId", "?"),
                        report_type,
                        self._fundamentals_timeout_s,
                    )
                    return _FundamentalResult(xml=None, reject=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "reqFundamentalData(conid=%s, report=%s) ib_async-Fehler: %s",
                getattr(contract, "conId", "?"),
                report_type,
                exc,
            )
            raise _map_ib_error(exc) from exc

        fatal = first_fatal_ib_error(collected_errors, req_id)
        if fatal is not None:
            logger.info(
                "reqFundamentalData(conid=%s, report=%s) IBKR-Reject (code=%s): %s",
                getattr(contract, "conId", "?"),
                report_type,
                fatal[0],
                fatal[1],
            )
            return _FundamentalResult(xml=None, reject=fatal)

        # ib_async loest das Future bei RaiseRequestErrors=False mit dem leeren
        # Container ([]) auf, wenn kein fundamentalData-Callback kam — das ist
        # KEIN str und darf nicht als Report-XML durchgehen. Ein leerer String
        # zaehlt ebenfalls als "kein Report".
        if not isinstance(raw, str) or not raw.strip():
            return _FundamentalResult(xml=None, reject=None)
        return _FundamentalResult(xml=raw, reject=None)

    async def _import_contract_class(self) -> Any:
        try:
            from ib_async.contract import Contract  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "code": "ib_async_missing",
                    "message": "ib_async ist nicht installiert",
                },
            )
        return Contract


def _map_historical_error_code(code: int, message: str) -> HTTPException:
    """Mappt einen IBKR-Historik-/Fundamentals-Fehlercode auf HTTPException.

    Exakter Code-Vergleich, kein Substring: Error 162 (Pacing) -> 429. Der
    Code 10162 ('market data not subscribed') enthaelt den Teilstring '162',
    ist aber ein Berechtigungs-/Verfuegbarkeitsfehler und darf NICHT als Retry
    (429) erscheinen -> 404. Error 200 ('no security definition') -> 404
    contract_not_found. Jeder andere fatale Code -> 404 mit dem echten
    ``ibkr_code`` im Detail, damit der Konsument die Ursache kennt und einen
    Reject von legitim leeren Daten unterscheiden kann.

    Bewusste Divergenz zum Scanner: derselbe IBKR-Code kann je Endpunkt einen
    anderen HTTP-Status ergeben (z.B. 10162 -> hier 404, im Scanner 422), weil
    /historical und /fundamentals eine Daten-Ressource modellieren (fehlende
    Verfuegbarkeit -> 404) und der Scanner eine Anfrage (Reject -> 422). Der
    Konsument disambiguiert ueber das Body-Feld ``code``/``ibkr_code``, nicht
    ueber den nackten HTTP-Status.
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
    if code == 200:
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "contract_not_found",
                "message": "IBKR liefert keine Security-Definition fuer diesen conid",
                "ibkr_code": 200,
            },
        )
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": "historical_data_unavailable",
            "message": (
                f"IBKR lehnt die Historik-/Fundamentals-Anfrage ab "
                f"(Code {code}): {message}"
            ),
            "ibkr_code": code,
        },
    )


def _map_ib_error(exc: Exception) -> HTTPException:
    """Mappt eine echte ib_async-Exception auf HTTPException.

    Der IBKR-Business-Fehlerpfad laeuft ueber errorEvent +
    _map_historical_error_code (ib_async wirft mit RaiseRequestErrors=False
    nicht). Diese Funktion greift nur fuer echte Exceptions: eine
    ``RequestError`` (falls RaiseRequestErrors doch aktiviert ist) traegt ein
    ``.code`` und wird ueber ihren exakten Code gemappt; alles andere
    (ConnectionError, Socket-Fehler) auf 502.
    """
    code = getattr(exc, "code", None)
    if isinstance(code, int):
        return _map_historical_error_code(code, str(exc))
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": "ib_async_error", "message": str(exc)},
    )


def validate_what_to_show(value: str) -> str:
    """Validiert den ``?whatToShow=``-Query-Param gegen die Whitelist.

    Gibt den Wert unveraendert zurueck, wenn er in
    ``ALLOWED_WHAT_TO_SHOW`` enthalten ist (exakter, case-sensitiver
    Match wie bei den Reuters-Report-Typen). Andernfalls HTTP 422 mit
    ``code=unsupported_what_to_show`` nach dem Error-Modell (Section
    1.6) — analog zur Fundamentals-Whitelist.
    """
    if value not in ALLOWED_WHAT_TO_SHOW:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "unsupported_what_to_show",
                "message": (
                    f"whatToShow={value!r} nicht unterstuetzt. Erlaubt: "
                    f"{sorted(ALLOWED_WHAT_TO_SHOW)}"
                ),
            },
        )
    return value


def parse_report_types(raw: str | None) -> list[str]:
    """Parsing fuer den ``?report_types=A,B,C``-Query-Param.

    ``None`` bzw. leer → Default-Set. Whitespace toleriert. Duplikate
    werden bewahrt — der Service ruft jeden Report-Typ einmal, leichte
    Doppel-Anfragen sind nicht kritisch (Cache nicht hier).
    """
    if not raw or not raw.strip():
        return list(DEFAULT_FUNDAMENTAL_REPORTS)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or list(DEFAULT_FUNDAMENTAL_REPORTS)
