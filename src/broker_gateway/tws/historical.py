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
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

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
        what_to_show: str = "TRADES",
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
        any_reuters_error = False
        for report_type in report_types:
            xml = await self._fetch_one_fundamental(contract, report_type)
            if xml is None:
                any_reuters_error = True
                continue
            records.append(FundamentalReport(report_type=report_type, xml=xml))

        if not records and any_reuters_error:
            # Kein einziger Report kam zurueck → Reuters-Berechtigung
            # fehlt komplett oder Symbol hat keine Coverage.
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
        first = results[0]
        if isinstance(first, list):
            if not first:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={
                        "code": "contract_not_found",
                        "message": f"conid {conid} unbekannt",
                    },
                )
            return first[0]
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
        async with self._historical_lock:
            wait = self._historical_pacing_s - (
                time.monotonic() - self._last_historical_at
            )
            if wait > 0:
                await asyncio.sleep(wait)
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
        return list(bars or [])

    async def _fetch_one_fundamental(
        self, contract: Any, report_type: str
    ) -> str | None:
        """Holt einen Reuters-Report. None signalisiert "fehlt".

        Wir kapseln pro Report einen Timeout + Exception-Pfad, damit ein
        einzelner Reuters-Hicks die Gesamtantwort nicht killt. Bei
        fehlender Reuters-Berechtigung liefert IBKR Error 430 oder
        einen leeren String — beides mappt der Aufrufer auf
        ``records``-Ausschluss.
        """
        ib = self._client._ib  # noqa: SLF001
        try:
            xml = await asyncio.wait_for(
                ib.reqFundamentalDataAsync(contract, report_type),
                timeout=self._fundamentals_timeout_s,
            )
        except TimeoutError:
            logger.warning(
                "reqFundamentalDataAsync(conid=%s, report=%s) Timeout nach %.1fs",
                getattr(contract, "conId", "?"),
                report_type,
                self._fundamentals_timeout_s,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            # Reuters-Berechtigung fehlt o.ae. — Report ausschliessen,
            # aber andere Reports weiter versuchen.
            logger.info(
                "reqFundamentalDataAsync(conid=%s, report=%s) ohne Daten: %s",
                getattr(contract, "conId", "?"),
                report_type,
                exc,
            )
            return None
        if xml is None:
            return None
        text = str(xml)
        # IBKR liefert manchmal einen leeren String statt eines Errors,
        # wenn der Report nicht verfuegbar ist. Den ebenfalls als
        # "fehlt" zaehlen, damit der 404-Pfad sauber greift.
        if not text.strip():
            return None
        return text

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


def _map_ib_error(exc: Exception) -> HTTPException:
    """Mappt typische ib_async-Fehler auf HTTPException.

    IBKR-Error-Code 162 (Pacing Violation): 429.
    IBKR-Error-Code 200 (No security definition): 404.
    Sonst: 502.
    """
    message = str(exc)
    if "162" in message or "pacing" in message.lower():
        return HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "ibkr_pacing_violation",
                "message": (
                    "IBKR-Pacing-Limit erreicht (Error 162). Spaeter erneut."
                ),
            },
        )
    if "200" in message or "no security definition" in message.lower():
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "contract_not_found",
                "message": "IBKR liefert keine Security-Definition fuer diesen conid",
            },
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail={"code": "ib_async_error", "message": message},
    )


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
