"""EventBus fuer SSE-Stream `/v1/events/stream`.

Aggregiert die drei Event-Typen, die der trading-robot konsumiert:

- `execution`        - Order-Ausfuehrung (Fill / Partial-Fill)
- `position`         - Positionsaenderung
- `status`           - Order-Status-Wechsel (Submitted, Cancelled, Rejected, ...)

Architektur:

- **Singleton-EventBus** pro App-Instanz. Quelle der Events ist eine
  asynchrone "Source" (in Karte 11 ein Mock; eine spaetere Karte ersetzt
  sie durch den CP-Gateway-WebSocket gegen `/v1/api/ws`).
- Alle SSE-Consumer fan'en aus dem Bus heraus: jede neue Event geht in
  die Queue jedes verbundenen Consumers gleichzeitig.
- Ringpuffer (Default 200 Events) erlaubt Reconnect via Last-Event-ID.

Single Source of Truth fuer Event-Schema: dieses Modul. Andere Module
duerfen Events nur als `Event`-Instanzen weiterreichen.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, Literal

from pydantic import BaseModel


logger = logging.getLogger(__name__)


_DEFAULT_REPLAY_BUFFER_SIZE = 200
_CONSUMER_QUEUE_MAX = 1024


EventType = Literal["execution", "position", "status"]
ALL_EVENT_TYPES: frozenset[str] = frozenset({"execution", "position", "status"})


class ExecutionEvent(BaseModel):
    """Order-Ausfuehrung (Fill oder Partial-Fill)."""

    order_id: str
    account_id: str | None = None
    conid: int | None = None
    symbol: str | None = None
    side: str | None = None
    filled_quantity: str
    avg_fill_price: str | None = None
    status: str = "Filled"
    occurred_at: datetime


class PositionEvent(BaseModel):
    """Positionsaenderung nach Ausfuehrung."""

    account_id: str
    conid: int
    symbol: str | None = None
    new_quantity: str
    change: str | None = None
    occurred_at: datetime


class StatusEvent(BaseModel):
    """Order-Status-Wechsel (Submitted -> Filled, Cancelled, ...)."""

    order_id: str
    account_id: str | None = None
    old_status: str | None = None
    new_status: str
    reason: str | None = None
    occurred_at: datetime


EventBody = ExecutionEvent | PositionEvent | StatusEvent


@dataclass(frozen=True)
class Event:
    """Generischer SSE-Event-Wrapper.

    `event_id` ist monoton steigend pro EventBus und dient als
    `Last-Event-ID` fuer Reconnect.
    """

    event_id: int
    event_type: EventType
    body: EventBody

    def to_sse_payload(self) -> str:
        return (
            f"id: {self.event_id}\n"
            f"event: {self.event_type}\n"
            f"data: {_json_dumps(self.body.model_dump(mode='json'))}\n\n"
        )


def _json_dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unserialisierbarer Typ: {type(value).__name__}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---- Source-Abstraktion ----

EventSource = Callable[["EventBus"], Awaitable[None]]
"""Eine Source ist eine async-Coroutine, die Events ueber `bus.publish_*`
einschiebt. Sie laeuft im Hintergrund (asyncio.Task) und wird beim
Shutdown abgebrochen."""


@dataclass
class _Consumer:
    consumer_id: str
    types: frozenset[str]
    queue: asyncio.Queue[Event | None] = field(
        default_factory=lambda: asyncio.Queue(maxsize=_CONSUMER_QUEUE_MAX)
    )


class EventBus:
    """Singleton-Bus fuer Events.

    Es existiert pro App-Lifespan genau eine Instanz. Tests ueberschreiben
    sie via `app.dependency_overrides[get_event_bus] = ...`.
    """

    def __init__(
        self,
        *,
        replay_buffer_size: int = _DEFAULT_REPLAY_BUFFER_SIZE,
    ) -> None:
        self._replay_buffer_size = replay_buffer_size
        self._consumers: dict[str, _Consumer] = {}
        self._buffer: collections.deque[Event] = collections.deque(maxlen=replay_buffer_size)
        self._next_event_id = 0
        self._lock = asyncio.Lock()
        self._source_task: asyncio.Task[None] | None = None
        self._stopped = False

    # ---- Lifecycle ----

    def start_source(self, source: EventSource) -> None:
        """Startet eine Hintergrund-Source als asyncio-Task."""
        if self._source_task is not None:
            return
        self._source_task = asyncio.create_task(
            source(self), name="event-bus-source"
        )

    async def shutdown(self) -> None:
        self._stopped = True
        task = self._source_task
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            self._source_task = None
        async with self._lock:
            consumers = list(self._consumers.values())
            self._consumers.clear()
        for consumer in consumers:
            try:
                consumer.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass

    # ---- Publish ----

    async def publish(self, event_type: EventType, body: EventBody) -> Event:
        async with self._lock:
            event = Event(
                event_id=self._next_event_id,
                event_type=event_type,
                body=body,
            )
            self._next_event_id += 1
            self._buffer.append(event)
            consumers = list(self._consumers.values())

        for consumer in consumers:
            if event_type not in consumer.types:
                continue
            _enqueue(consumer.queue, event)
        return event

    async def publish_execution(self, body: ExecutionEvent) -> Event:
        return await self.publish("execution", body)

    async def publish_position(self, body: PositionEvent) -> Event:
        return await self.publish("position", body)

    async def publish_status(self, body: StatusEvent) -> Event:
        return await self.publish("status", body)

    # ---- Subscribe ----

    async def subscribe(
        self,
        *,
        consumer_id: str,
        types: frozenset[str] | None = None,
        last_event_id: int | None = None,
    ) -> AsyncIterator[Event]:
        normalized_types = (
            frozenset(t for t in (types or ALL_EVENT_TYPES) if t in ALL_EVENT_TYPES)
        )
        if not normalized_types:
            normalized_types = ALL_EVENT_TYPES

        consumer = _Consumer(consumer_id=consumer_id, types=normalized_types)

        async with self._lock:
            self._consumers[consumer_id] = consumer
            replay = [
                ev
                for ev in self._buffer
                if ev.event_type in normalized_types
                and (last_event_id is None or ev.event_id > last_event_id)
            ]

        async def _iterator() -> AsyncIterator[Event]:
            try:
                for replay_event in replay:
                    yield replay_event
                while True:
                    event = await consumer.queue.get()
                    if event is None:
                        return
                    yield event
            finally:
                async with self._lock:
                    self._consumers.pop(consumer_id, None)

        return _iterator()

    # ---- Inspection (fuer Tests) ----

    @property
    def consumer_count(self) -> int:
        return len(self._consumers)

    @property
    def buffer_size(self) -> int:
        return len(self._buffer)


def _enqueue(queue: asyncio.Queue[Event | None], event: Event) -> None:
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        # Slow-Consumer: aeltesten Event verwerfen, dann erneut.
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Consumer-Queue weiterhin voll - Event verworfen")


# ---- FastAPI-Dependency ----


def get_event_bus() -> EventBus:
    raise RuntimeError(
        "get_event_bus muss in der App per dependency_overrides gesetzt werden"
    )


__all__ = [
    "ALL_EVENT_TYPES",
    "Event",
    "EventBody",
    "EventBus",
    "EventSource",
    "EventType",
    "ExecutionEvent",
    "PositionEvent",
    "StatusEvent",
    "get_event_bus",
    "utcnow",
]
