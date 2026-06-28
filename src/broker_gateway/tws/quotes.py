"""Quotes-Adapter gegen die TWS-Socket-API (ib_async).

Pendant zu :class:`broker_gateway.cp.quotes.QuotesService` plus
:class:`broker_gateway.streams.manager.SubscriptionManager`. Der
TWS-Pfad bedient sowohl den REST-Snapshot (/v1/quotes/snapshot) als
auch den Live-Stream (/v1/quotes/stream + /v1/quotes/ws) aus einem
Service-Objekt.

Schema-Identitaet: liefert dieselben Pydantic-Modelle (``Quote``) und
dieselben IBKR-cp-Field-IDs (``31``=Last, ``84``=Bid, ``86``=Ask,
``7762``=Volume, ``83``=Change-%, ``70``=High, ``71``=Low,
``6509``=Availability), damit Konsumenten den Backend-Switch nicht
sehen.

ib_async-Patterns (siehe Memory ``project_tws_api_pi5_setup``):

- ``IB.reqTickersAsync(*contracts)`` → ``list[Ticker]`` (Snapshot,
  wartet intern bis die Daten da sind).
- ``IB.reqMktData(contract, snapshot=False)`` → laufende Subscription;
  ``IB.cancelMktData(contract)`` zum Beenden.
- ``IB.pendingTickersEvent`` feuert mit ``set[Ticker]`` bei jedem
  Pricing-Update-Batch - das ist der Push-Pfad fuer den Live-Stream.

AP ``2a203c58-...`` Phase 3.
"""
from __future__ import annotations

import asyncio
import collections
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, AsyncIterator

from fastapi import HTTPException, status

from broker_gateway.availability import map_availability
from broker_gateway.cp.quotes import (
    FIELD_ALIASES,
    Quote,
    normalize_default_fields,
    resolve_fields,
)
from broker_gateway.streams.manager import StreamEvent

if TYPE_CHECKING:
    from broker_gateway.tws.client import TWSClient


logger = logging.getLogger(__name__)


__all__ = [
    "TWSQuotesService",
    "Quote",
    "FIELD_ALIASES",
    "normalize_default_fields",
    "resolve_fields",
]


_AVAILABILITY_CODE = "6509"
_DEFAULT_SNAPSHOT_TIMEOUT_S = 11.5  # IBKR cap=11s, kleiner Puffer
_DEFAULT_MARKET_DATA_TYPE = 3  # delayed-frozen, robust gegen Sub-Konflikte
_REPLAY_BUFFER_SIZE = 200
_CONSUMER_QUEUE_MAX = 1024


# Kanonisches Mapping ib_async-Ticker-Attribut -> cp-Field-ID.
# Die rechte Seite muss exakt mit cp.quotes.FIELD_ALIASES korrespondieren,
# sonst sehen Konsumenten Schema-Drift beim Backend-Switch.
_TICKER_FIELD_MAP: dict[str, str] = {
    "last": "31",
    "bid": "84",
    "ask": "86",
    "volume": "7762",
    "high": "70",
    "low": "71",
}


class TWSQuotesService:
    """ib_async-basierter Quotes-Service.

    Liefert das gleiche Pydantic-Modell und dasselbe Field-ID-Schema
    wie ``cp.QuotesService``. Snapshot via ``reqTickersAsync``,
    Live-Stream via ``reqMktData`` + ``pendingTickersEvent`` mit
    Refcount + Fan-Out + Ringpuffer-Replay analog
    ``SubscriptionManager``.
    """

    def __init__(
        self,
        client: "TWSClient",
        *,
        market_data_type: int = _DEFAULT_MARKET_DATA_TYPE,
        snapshot_timeout_s: float = _DEFAULT_SNAPSHOT_TIMEOUT_S,
        replay_buffer_size: int = _REPLAY_BUFFER_SIZE,
    ) -> None:
        self._client = client
        self._market_data_type = market_data_type
        self._snapshot_timeout_s = snapshot_timeout_s
        self._replay_buffer_size = replay_buffer_size
        self._stream_lock = asyncio.Lock()
        self._stream_subs: dict[int, _ConidStreamSub] = {}
        # Globaler pendingTickersEvent-Listener wird beim ersten subscribe()
        # registriert. ib_async-Events sind set[Ticker], wir routen pro
        # Ticker.contract.conId an die zustaendige _ConidStreamSub.
        self._pending_listener_attached = False

    # ---- Snapshot ----------------------------------------------------

    async def snapshot_with_prime(
        self,
        conids: list[int],
        fields: list[str],
    ) -> list[Quote]:
        """One-shot Snapshot fuer bis zu N conids.

        Pro conid wird der Contract qualifiziert und per ``reqTickersAsync``
        gesnapshottet - ib_async wartet intern, bis die Snapshot-Daten da
        sind, und IBKR cancelt die Subscription danach automatisch. Bei
        Timeout oder leerer Ticker-Liste wird der conid uebersprungen,
        nicht 504 - analog zum cp-Verhalten, das auch leere Felder als
        gueltige Antwort gibt.

        ``fields`` (cp-Field-IDs) wird nicht zur Filterung verwendet -
        ib_async liefert immer die voll bestueckten Ticker-Attribute,
        und das Pydantic-``Quote``-Modell unterscheidet die Felder
        ohnehin. Der Parameter ist nur fuer Schema-Kompatibilitaet zur
        cp-Variante da.
        """
        if not conids:
            return []
        ib = self._client._ib  # noqa: SLF001
        ib.reqMarketDataType(self._market_data_type)
        contract_class = await self._import_contract_class()

        results: list[Quote] = []
        for cid in conids:
            qualified = await self._qualify(contract_class, int(cid))
            if qualified is None:
                continue
            try:
                # reqTickersAsync ist der async-Snapshot-Weg in ib_async
                # (das frueher genutzte reqMktDataAsync existiert nicht).
                # Es liefert eine list[Ticker] und wartet intern, bis die
                # Snapshot-Daten da sind; IBKR cancelt die Subscription
                # danach automatisch.
                tickers = await asyncio.wait_for(
                    ib.reqTickersAsync(qualified, regulatorySnapshot=False),
                    timeout=self._snapshot_timeout_s,
                )
            except (TimeoutError, asyncio.TimeoutError):
                logger.warning(
                    "TWSQuotesService snapshot timeout fuer conid=%s", cid
                )
                continue
            if not tickers:
                logger.warning(
                    "TWSQuotesService snapshot: keine Ticker-Daten fuer conid=%s",
                    cid,
                )
                continue
            results.append(
                _quote_from_ticker(int(cid), tickers[0], self._market_data_type)
            )
        return results

    # ---- Stream (SubscriptionManager-kompatibel) ---------------------

    async def subscribe(
        self,
        conids: list[int],
        field_codes: list[str],
        consumer_id: str,
        *,
        last_event_id: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Live-Stream-Subscription, signature-kompatibel zu
        :py:meth:`SubscriptionManager.subscribe`.

        Refcount auf conid-Ebene: mehrere Consumer auf demselben conid
        teilen sich genau einen ib_async-Ticker. Cleanup
        (``cancelMktData``) sobald der letzte Consumer den Iterator
        verlaesst.
        """
        if not conids:
            raise ValueError("conids darf nicht leer sein")

        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue(
            maxsize=_CONSUMER_QUEUE_MAX
        )
        attached: list[_ConidStreamSub] = []

        ib = self._client._ib  # noqa: SLF001
        ib.reqMarketDataType(self._market_data_type)
        contract_class = await self._import_contract_class()

        async with self._stream_lock:
            self._ensure_listener_attached()
            for cid in conids:
                cid_int = int(cid)
                sub = self._stream_subs.get(cid_int)
                if sub is None:
                    qualified = await self._qualify(contract_class, cid_int)
                    if qualified is None:
                        # Unbekannter conid: skip - Konsumenten sehen keine
                        # Frames fuer diesen conid, aber der Iterator laeuft
                        # weiter fuer die uebrigen conids.
                        continue
                    ticker = ib.reqMktData(
                        qualified, "", snapshot=False, regulatorySnapshot=False
                    )
                    sub = _ConidStreamSub(
                        conid=cid_int,
                        contract=qualified,
                        ticker=ticker,
                        replay_buffer_size=self._replay_buffer_size,
                    )
                    self._stream_subs[cid_int] = sub
                sub.attach_consumer(consumer_id, queue)
                attached.append(sub)

        async def _iterator() -> AsyncIterator[StreamEvent]:
            try:
                # Replay aus Ringpuffern (Reconnect-Continuity).
                for sub in attached:
                    for buffered in list(sub.event_buffer):
                        if last_event_id is None or buffered.event_id > last_event_id:
                            yield buffered
                while True:
                    event = await queue.get()
                    if event is None:
                        return
                    yield event
            finally:
                await self._detach_consumer(consumer_id, attached)

        return _iterator()

    # ---- Lifecycle ---------------------------------------------------

    async def shutdown(self) -> None:
        """Cleanup aller offenen Subscriptions (Lifespan-Stop)."""
        async with self._stream_lock:
            subs = list(self._stream_subs.values())
            self._stream_subs.clear()
        ib = self._client._ib  # noqa: SLF001
        for sub in subs:
            try:
                ib.cancelMktData(sub.contract)
            except Exception as exc:  # noqa: BLE001
                logger.info(
                    "cancelMktData conid=%s waehrend shutdown fehlgeschlagen: %s",
                    sub.conid,
                    exc,
                )
            for queue in list(sub.consumers.values()):
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass
        if self._pending_listener_attached:
            try:
                ib.pendingTickersEvent -= self._on_pending_tickers
            except Exception:  # noqa: BLE001 - best effort
                pass
            self._pending_listener_attached = False

    # ---- Helpers -----------------------------------------------------

    @property
    def active_conids(self) -> set[int]:
        return set(self._stream_subs.keys())

    def _ensure_listener_attached(self) -> None:
        if self._pending_listener_attached:
            return
        ib = self._client._ib  # noqa: SLF001
        ib.pendingTickersEvent += self._on_pending_tickers
        self._pending_listener_attached = True

    def _on_pending_tickers(self, tickers: Any) -> None:
        # ib_async feuert mit set[Ticker]. Wir routen pro Ticker an die
        # zustaendige _ConidStreamSub und ignorieren Tickers ohne
        # bekannten conid-Slot (z.B. Snapshot-Calls, die diesen Listener
        # auch triggern koennen).
        for ticker in tickers:
            contract = getattr(ticker, "contract", None)
            if contract is None:
                continue
            cid = getattr(contract, "conId", None)
            if not cid:
                continue
            sub = self._stream_subs.get(int(cid))
            if sub is None:
                continue
            quote = _quote_from_ticker(int(cid), ticker, self._market_data_type)
            sub.push_event(quote)

    async def _detach_consumer(
        self, consumer_id: str, attached: list[_ConidStreamSub]
    ) -> None:
        async with self._stream_lock:
            for sub in attached:
                sub.detach_consumer(consumer_id)
                if sub.refcount == 0:
                    self._stream_subs.pop(sub.conid, None)
                    ib = self._client._ib  # noqa: SLF001
                    try:
                        ib.cancelMktData(sub.contract)
                    except Exception as exc:  # noqa: BLE001
                        logger.info(
                            "cancelMktData conid=%s nach Detach fehlgeschlagen: %s",
                            sub.conid,
                            exc,
                        )

    async def _qualify(self, contract_class: Any, cid: int) -> Any | None:
        ib = self._client._ib  # noqa: SLF001
        contract = contract_class(conId=cid, exchange="SMART")
        try:
            qualified = await ib.qualifyContractsAsync(contract)
        except Exception as exc:  # noqa: BLE001
            logger.warning("qualifyContractsAsync conid=%s gescheitert: %s", cid, exc)
            return None
        if not qualified:
            return None
        first = qualified[0]
        if isinstance(first, list):
            if not first or first[0] is None:
                return None
            return first[0]
        return first

    async def _import_contract_class(self) -> Any:
        try:
            from ib_async.contract import Contract  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="ib_async ist nicht installiert",
            )
        return Contract


class _ConidStreamSub:
    """Interne Buchhaltung pro Live-Stream-conid."""

    def __init__(
        self,
        conid: int,
        contract: Any,
        ticker: Any,
        *,
        replay_buffer_size: int,
    ) -> None:
        self.conid = conid
        self.contract = contract
        self.ticker = ticker
        self.consumers: dict[str, asyncio.Queue[StreamEvent | None]] = {}
        self.event_buffer: collections.deque[StreamEvent] = collections.deque(
            maxlen=replay_buffer_size
        )
        self._next_event_id = 0

    @property
    def refcount(self) -> int:
        return len(self.consumers)

    def attach_consumer(
        self,
        consumer_id: str,
        queue: asyncio.Queue[StreamEvent | None],
    ) -> None:
        self.consumers[consumer_id] = queue

    def detach_consumer(self, consumer_id: str) -> None:
        self.consumers.pop(consumer_id, None)

    def push_event(self, quote: Quote) -> StreamEvent:
        event = StreamEvent(
            event_id=self._next_event_id,
            conid=quote.conid,
            quote=quote,
        )
        self._next_event_id += 1
        self.event_buffer.append(event)
        for queue in list(self.consumers.values()):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: aeltesten Event verwerfen, dann erneut.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    logger.warning(
                        "Consumer-Queue weiterhin voll - Event verworfen"
                    )
        return event


# ---- Ticker -> Quote-Mapping -----------------------------------------


def _quote_from_ticker(conid: int, ticker: Any, market_data_type: int) -> Quote:
    """Mappt einen ib_async-Ticker auf das cp-Quote-Schema.

    NaN-Werte (Pandas/Numpy-Konvention bei "kein Wert") werden auf
    None gemappt, damit das Pydantic-Schema dem cp-Pfad entspricht.
    """
    last = _str_or_none(getattr(ticker, "last", None))
    bid = _str_or_none(getattr(ticker, "bid", None))
    ask = _str_or_none(getattr(ticker, "ask", None))
    high = _str_or_none(getattr(ticker, "high", None))
    low = _str_or_none(getattr(ticker, "low", None))
    volume = _volume_from_ticker(getattr(ticker, "volume", None))
    change_pct = _change_pct_from_ticker(ticker)
    raw_avail, mapped_avail = _availability_for_market_data_type(market_data_type)
    updated_at = _ticker_time_to_datetime(getattr(ticker, "time", None))
    return Quote(
        conid=conid,
        last=last,
        bid=bid,
        ask=ask,
        volume=volume,
        change_pct=change_pct,
        high=high,
        low=low,
        availability=mapped_avail,
        availability_raw=raw_avail,
        updated_at=updated_at,
    )


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, float):
            # ib_async signalisiert "kein Wert" via NaN. NaN != NaN ist
            # die kanonische Erkennung ohne math-Import.
            if value != value:
                return None
            return str(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return None
    return str(value)


def _volume_from_ticker(value: Any) -> str | None:
    """Liefert Volume als String in Aktien-Anzahl.

    cp-Pfad teilt das rohe IBKR-Field 7762 durch 10^6 und liefert die
    Aktien-Anzahl als ``str(int)``. Im ib_async-Pfad gibt
    ``Ticker.volume`` die Aktien-Anzahl direkt (Float). Wir runden auf
    int und formattieren als String, damit das Schema beider Pfade
    identisch bleibt.
    """
    if value is None:
        return None
    try:
        raw = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if raw != raw:  # NaN
        return None
    if raw < 0:
        return None
    return str(int(raw))


def _change_pct_from_ticker(ticker: Any) -> str | None:
    """Berechnet Change-% aus close+last, wenn beide vorhanden sind.

    cp.quotes liefert die rohe IBKR-Zahl unter Field 83 (z.B. ``"0.42"``
    fuer 0.42%). ib_async hat kein direktes Aequivalent - wir leiten den
    Wert aus ``last`` und ``close`` ab. Falls ``close`` 0 oder fehlt,
    geben wir None zurueck.
    """
    last = getattr(ticker, "last", None)
    close = getattr(ticker, "close", None)
    if last is None or close is None:
        return None
    try:
        last_d = Decimal(str(last))
        close_d = Decimal(str(close))
    except (InvalidOperation, ValueError):
        return None
    if close_d == 0:
        return None
    if last_d != last_d or close_d != close_d:  # NaN
        return None
    pct = (last_d - close_d) / close_d * Decimal(100)
    quantized = pct.quantize(Decimal("0.01"))
    return str(quantized)


def _availability_for_market_data_type(
    market_data_type: int,
) -> tuple[str | None, Any]:
    """Liefert ``(raw_code, mapped)`` analog zum cp-Field-6509.

    IBKR market-data-type:
    - 1 = Realtime (RPB)
    - 2 = Frozen (ZB)
    - 3 = Delayed (DPB)
    - 4 = Delayed-Frozen (YPB)
    """
    code = {
        1: "RPB",
        2: "ZB",
        3: "DPB",
        4: "YPB",
    }.get(market_data_type)
    if code is None:
        return None, None
    return code, map_availability(code)


def _ticker_time_to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    return None
