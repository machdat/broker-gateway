"""Subscription-Manager für SSE-Quote-Streams.

Single-Owner-Eigenschaft der IBKR-Session bedeutet: pro `conid` darf es
serverseitig **genau eine** Subscription geben, auch wenn N Consumer
parallel auf denselben conid hören. Refcount + Fan-Out leistet das.

Architektur:

- Pro aktiver `conid` lebt eine `ConidSubscription` mit
  - asyncio-Poll-Task gegen `/iserver/marketdata/snapshot` (klassischer
    Polling-Modus) **oder** Push-Modus, in dem Frames extern via
    :py:meth:`SubscriptionManager.publish` reingereicht werden,
  - Ringpuffer der letzten N Events für Reconnect via Last-Event-ID,
  - Set aktiver Consumer-Queues; jede neue Quote wird gleichzeitig in
    alle Queues gepusht.
- Bei `refcount=0` startet ein Cool-Down-Task; läuft er ab, wird das
  CP-Gateway per `/iserver/marketdata/{conid}/unsubscribe` informiert
  und die ConidSubscription weggeräumt. Connectet ein neuer Consumer
  binnen Cool-Down, wird der Task gecancelt - kein unnötiges Re-Subscribe.

Quotes-Quelle (``polling`` vs. ``ws``)
--------------------------------------

Pro Manager-Instanz gilt **eine** Quelle. Im ``polling``-Modus startet
``_ConidSubscription`` einen REST-Poll-Task pro conid (Verhalten bis
Phase A des K6-Designs). Im ``ws``-Modus wird **kein** Poll-Task
gestartet; stattdessen feedet der :py:class:`broker_gateway.streams.ws_source.WSPushSource`
Quotes ueber ``publish(conid, quote)`` ein. Der zugehoerige
Subscribe-Frame an das CP-Gateway laeuft ueber den WS-Client und die
:py:class:`broker_gateway.streams.registry.SubscriptionRegistry`, nicht
ueber den Manager - das ist Aufgabe der WSPushSource. Konfiguration via
ENV ``BG_QUOTES_SOURCE`` (siehe ``broker_gateway.config.quotes_source``).
"""
from __future__ import annotations

import asyncio
import collections
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Literal

from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.cp.quotes import (
    FIELD_ALIASES,
    Quote,
    _quote_from_entry,
)


QuotesSourceMode = Literal["polling", "ws"]
WSSubscribeCallable = Callable[[int, set[str]], Awaitable[None]]
WSUnsubscribeCallable = Callable[[int], Awaitable[None]]


logger = logging.getLogger(__name__)


_DEFAULT_POLL_INTERVAL_S = 1.0
_DEFAULT_COOL_DOWN_S = 5.0
_DEFAULT_MAX_CONIDS = 250
_AVAILABILITY_CODE = "6509"
_REPLAY_BUFFER_SIZE = 200
_CONSUMER_QUEUE_MAX = 1024


class SubscriptionLimitExceeded(Exception):
    """Manager hat das CP-Gateway-Limit (max parallel conids) erreicht."""


@dataclass(frozen=True)
class StreamEvent:
    event_id: int
    conid: int
    quote: Quote
    event_type: str = "quote"

    def to_sse_payload(self) -> str:
        """Renderts als SSE-Frame (`id:`/`event:`/`data:`/Leerzeile)."""
        body = self.quote.model_dump(mode="json")
        return (
            f"id: {self.event_id}\n"
            f"event: {self.event_type}\n"
            f"data: {_json_dumps(body)}\n\n"
        )


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unserialisierbarer Typ: {type(value).__name__}")


class _ConidSubscription:
    """Interne Buchführung für genau einen conid."""

    def __init__(
        self,
        manager: "SubscriptionManager",
        conid: int,
        field_codes: set[str],
    ) -> None:
        self._manager = manager
        self.conid = conid
        self.field_codes: set[str] = set(field_codes)
        self.consumers: dict[str, asyncio.Queue[StreamEvent | None]] = {}
        self.event_buffer: collections.deque[StreamEvent] = collections.deque(
            maxlen=_REPLAY_BUFFER_SIZE
        )
        self._next_event_id = 0
        self._poll_task: asyncio.Task[None] | None = None
        self._cool_down_task: asyncio.Task[None] | None = None
        self._stopped = False

    @property
    def refcount(self) -> int:
        return len(self.consumers)

    def attach_consumer(
        self,
        consumer_id: str,
        queue: asyncio.Queue[StreamEvent | None],
    ) -> None:
        self._cancel_cool_down_locked()
        self.consumers[consumer_id] = queue

    def detach_consumer(self, consumer_id: str) -> None:
        self.consumers.pop(consumer_id, None)
        if self.refcount == 0:
            self._cool_down_task = asyncio.create_task(
                self._cool_down_then_teardown(),
                name=f"cp-cool-down-{self.conid}",
            )

    def _cancel_cool_down_locked(self) -> None:
        task = self._cool_down_task
        if task is not None and not task.done():
            task.cancel()
        self._cool_down_task = None

    async def start_poll(self) -> None:
        if self._poll_task is not None:
            return
        if self._manager.quotes_source != "polling":
            # Im ``ws``-Modus uebernimmt der externe WSPushSource die
            # Frame-Belieferung via ``publish(conid, quote)``; ein Poll-
            # Task waere doppelte Last und Last-Last-Wettrennen.
            return
        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name=f"cp-poll-{self.conid}",
        )

    def push_quote(self, quote: Quote) -> StreamEvent:
        """Konvertiert eine extern gelieferte Quote in einen StreamEvent
        und schiebt ihn in Ringpuffer + alle Consumer-Queues.

        Genutzt vom WSPushSource im ``ws``-Modus, um Frames in den
        bestehenden Fan-Out einzuspeisen. Liefert das gebaute Event
        zurueck (fuer Tests).
        """
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

    async def _poll_loop(self) -> None:
        try:
            while not self._stopped:
                try:
                    await self._poll_once()
                except Exception as exc:  # nosec - poll-Fehler dürfen die Schleife nicht killen
                    logger.warning("poll-loop conid=%s fehlgeschlagen: %s", self.conid, exc)
                try:
                    await asyncio.sleep(self._manager.poll_interval_s)
                except asyncio.CancelledError:
                    return
        except asyncio.CancelledError:
            return

    async def _poll_once(self) -> None:
        params = {
            "conids": str(self.conid),
            "fields": ",".join(sorted(self.field_codes | {_AVAILABILITY_CODE})),
        }
        response = await self._manager._client.get("/iserver/marketdata/snapshot", params=params)
        if response.status_code != 200:
            return
        payload = response.json()
        if not isinstance(payload, list):
            return
        for entry in payload:
            if "conid" not in entry:
                continue
            quote = _quote_from_entry(entry)
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
                    # Slow consumer: ältesten Event verwerfen, dann erneut.
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        queue.put_nowait(event)
                    except asyncio.QueueFull:
                        logger.warning("Consumer-Queue weiterhin voll - Event verworfen")

    async def stop(self) -> None:
        self._stopped = True
        self._cancel_cool_down_locked()
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        # CP-Gateway darüber informieren - best-effort, Fehler werden geloggt.
        try:
            await self._manager._client.get(f"/iserver/marketdata/{self.conid}/unsubscribe")
        except Exception as exc:  # nosec
            logger.info("Unsubscribe conid=%s gescheitert: %s", self.conid, exc)
        # Sentinel an alle noch wartenden Consumer (sollte i.d.R. leer sein).
        for queue in list(self.consumers.values()):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    async def _cool_down_then_teardown(self) -> None:
        try:
            await asyncio.sleep(self._manager.cool_down_s)
        except asyncio.CancelledError:
            return
        if self.refcount > 0:
            return
        await self._manager._teardown(self)


class SubscriptionManager:
    def __init__(
        self,
        client: CPGatewayClient,
        *,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
        cool_down_s: float = _DEFAULT_COOL_DOWN_S,
        max_conids: int = _DEFAULT_MAX_CONIDS,
        quotes_source: QuotesSourceMode = "polling",
        ws_subscribe: WSSubscribeCallable | None = None,
        ws_unsubscribe: WSUnsubscribeCallable | None = None,
    ) -> None:
        if quotes_source == "ws" and ws_subscribe is None:
            raise ValueError(
                "quotes_source='ws' erfordert ein ws_subscribe-Callable"
            )
        self._client = client
        self.poll_interval_s = poll_interval_s
        self.cool_down_s = cool_down_s
        self.max_conids = max_conids
        self.quotes_source: QuotesSourceMode = quotes_source
        self._ws_subscribe = ws_subscribe
        self._ws_unsubscribe = ws_unsubscribe
        self._subs: dict[int, _ConidSubscription] = {}
        self._lock = asyncio.Lock()

    @property
    def active_conids(self) -> set[int]:
        return set(self._subs.keys())

    async def subscribe(
        self,
        conids: list[int],
        field_codes: list[str],
        consumer_id: str,
        *,
        last_event_id: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if not conids:
            raise ValueError("conids darf nicht leer sein")

        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue(maxsize=_CONSUMER_QUEUE_MAX)
        attached: list[_ConidSubscription] = []

        async with self._lock:
            new_conids = [c for c in conids if c not in self._subs]
            projected = len(self._subs) + len(new_conids)
            if projected > self.max_conids:
                raise SubscriptionLimitExceeded(
                    f"Aktive conids {len(self._subs)} + neue {len(new_conids)} "
                    f"ueberschreitet Limit {self.max_conids}"
                )
            ws_subscribe_calls: list[tuple[int, set[str]]] = []
            for conid in conids:
                sub = self._subs.get(conid)
                if sub is None:
                    sub = _ConidSubscription(self, conid, set(field_codes))
                    self._subs[conid] = sub
                    await sub.start_poll()
                    if self.quotes_source == "ws":
                        ws_subscribe_calls.append(
                            (conid, set(sub.field_codes))
                        )
                else:
                    sub.field_codes.update(field_codes)
                sub.attach_consumer(consumer_id, queue)
                attached.append(sub)
        # Ausserhalb des Locks: WS-Subscribe an das CP-Gateway absetzen.
        # Ein Send-Fehler hier darf den Consumer-Stream nicht killen -
        # die Registry haelt den Soll-State und replayt nach Reconnect.
        if ws_subscribe_calls and self._ws_subscribe is not None:
            for conid, fields in ws_subscribe_calls:
                try:
                    await self._ws_subscribe(conid, fields)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "WS-Subscribe conid=%s fehlgeschlagen: %s",
                        conid,
                        exc,
                    )

        async def _iterator() -> AsyncIterator[StreamEvent]:
            try:
                # Replay aus Ringpuffern (für Reconnect-Continuity).
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
                async with self._lock:
                    for sub in attached:
                        sub.detach_consumer(consumer_id)

        return _iterator()

    async def shutdown(self) -> None:
        async with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for sub in subs:
            await sub.stop()

    async def _teardown(self, sub: _ConidSubscription) -> None:
        async with self._lock:
            if sub.refcount > 0:
                return
            self._subs.pop(sub.conid, None)
        await sub.stop()
        if self.quotes_source == "ws" and self._ws_unsubscribe is not None:
            try:
                await self._ws_unsubscribe(sub.conid)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WS-Unsubscribe conid=%s fehlgeschlagen: %s",
                    sub.conid,
                    exc,
                )

    def publish(self, conid: int, quote: Quote) -> StreamEvent | None:
        """Speist eine extern gelieferte Quote in den Fan-Out des
        passenden conid-Slots ein.

        Genutzt vom WSPushSource (``BG_QUOTES_SOURCE=ws``). Liefert das
        gebaute Event zurueck oder ``None``, wenn fuer den conid keine
        aktive Subscription existiert (Frame wird verworfen, kein
        Fehler - der WS-Server kann Restframes nach Unsubscribe noch
        kurz nachsenden).
        """
        sub = self._subs.get(conid)
        if sub is None:
            return None
        return sub.push_quote(quote)


def get_subscription_manager() -> SubscriptionManager:
    raise RuntimeError(
        "get_subscription_manager muss in der App per dependency_overrides gesetzt werden"
    )
