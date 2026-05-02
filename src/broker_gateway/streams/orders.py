"""OrdersBroadcaster - Fan-Out fuer Live-Order-Updates.

Ein einzelner ``OrdersBroadcaster`` haelt pro ``account`` eine
Subscriber-Liste und pflegt einen Voll-Snapshot-Cache aller Orders.
Quellen:

- **REST-Bootstrap.** Beim Subscribe wird einmal
  ``CPGatewayClient.get(/iserver/account/orders)`` gerufen und das
  Ergebnis als initialer SSE-Frame an den Konsumenten geschickt.
  IBKR garantiert keinen Initial-Snapshot ueber WS, daher Pflicht-
  Vorlauf (K6-Sektion 5).
- **WS-Live-Push.** Frames vom ``SorTopicAdapter`` werden via
  ``publish(account, frame)`` in alle Consumer-Queues fan-outed.

Vertragsanker: SSE-Frame-Format ist semantisches JSON gemaess Anhang A
des K6-Designs. ``id``-/``event``-Header werden vom Endpoint-Layer
gerendert; der Broadcaster pflegt die Event-IDs und stellt einen
Ringpuffer pro Account fuer Last-Event-ID-Reconnect bereit.
"""
from __future__ import annotations

import asyncio
import collections
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, AsyncIterator

from broker_gateway.cp.topics.sor import SorFrame


logger = logging.getLogger(__name__)


_REPLAY_BUFFER_SIZE = 200
_CONSUMER_QUEUE_MAX = 1024


@dataclass(frozen=True)
class OrderStreamEvent:
    """SSE-Event aus dem ``/v1/orders/stream``-Pfad.

    ``payload`` ist das semantische Frame-JSON (Bootstrap oder Live).
    """

    event_id: int
    account: str
    event_type: str
    payload: dict[str, Any]


class _AccountSubscription:
    def __init__(self, account: str) -> None:
        self.account = account
        self._next_event_id = 0
        self.consumers: dict[str, asyncio.Queue[OrderStreamEvent | None]] = {}
        self.event_buffer: collections.deque[OrderStreamEvent] = (
            collections.deque(maxlen=_REPLAY_BUFFER_SIZE)
        )

    @property
    def refcount(self) -> int:
        return len(self.consumers)

    def attach(
        self,
        consumer_id: str,
        queue: asyncio.Queue[OrderStreamEvent | None],
    ) -> None:
        self.consumers[consumer_id] = queue

    def detach(self, consumer_id: str) -> None:
        self.consumers.pop(consumer_id, None)

    def publish(
        self, *, event_type: str, payload: dict[str, Any]
    ) -> OrderStreamEvent:
        event = OrderStreamEvent(
            event_id=self._next_event_id,
            account=self.account,
            event_type=event_type,
            payload=payload,
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
                        "OrdersBroadcaster: Consumer-Queue voll, Drop"
                    )
        return event


class OrdersBroadcaster:
    def __init__(self) -> None:
        self._subs: dict[str, _AccountSubscription] = {}
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        account: str,
        consumer_id: str,
        *,
        bootstrap: list[SorFrame] | None = None,
        last_event_id: int | None = None,
    ) -> AsyncIterator[OrderStreamEvent]:
        """Liefert einen Async-Iterator ueber Order-Events fuer das
        gegebene ``account``.

        Beim Subscribe wird der Bootstrap-Frame (sofern angegeben)
        zuerst eingespeist; danach folgen Live-Frames aus
        ``publish(...)``.
        """
        queue: asyncio.Queue[OrderStreamEvent | None] = asyncio.Queue(
            maxsize=_CONSUMER_QUEUE_MAX
        )
        async with self._lock:
            sub = self._subs.get(account)
            if sub is None:
                sub = _AccountSubscription(account)
                self._subs[account] = sub
            sub.attach(consumer_id, queue)
            # Bootstrap-Frame als ersten Snapshot publishen, sodass alle
            # Konsumenten ihn aus dem Ringpuffer-Replay sehen koennen.
            if bootstrap:
                sub.publish(
                    event_type="bootstrap",
                    payload={
                        "orders": [
                            _frame_to_payload(frame) for frame in bootstrap
                        ]
                    },
                )

        async def _iterator() -> AsyncIterator[OrderStreamEvent]:
            try:
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
                    sub.detach(consumer_id)
                    if sub.refcount == 0:
                        self._subs.pop(account, None)

        return _iterator()

    def publish(self, account: str, frame: SorFrame) -> None:
        """Speist ein Live-Order-Update in den Fan-Out ein.

        Wenn fuer ``account`` keine Subscription aktiv ist, wird das
        Frame still verworfen - der WS-Server kann nach einem
        Unsubscribe noch kurz nachsenden.
        """
        sub = self._subs.get(account)
        if sub is None:
            return
        sub.publish(event_type="order", payload=_frame_to_payload(frame))

    @property
    def active_accounts(self) -> set[str]:
        return set(self._subs.keys())


def _frame_to_payload(frame: SorFrame) -> dict[str, Any]:
    return {
        "order_id": frame.order_id,
        "account": frame.account,
        "client_order_id": frame.client_order_id,
        "parent_id": frame.parent_id,
        "symbol": frame.symbol,
        "side": frame.side,
        "quantity": _decimal_str(frame.quantity),
        "filled_quantity": _decimal_str(frame.filled_quantity),
        "avg_fill_price": _decimal_str(frame.avg_fill_price),
        "status": frame.status,
        "time_in_force": frame.time_in_force,
        "last_event_at": frame.last_event_at,
        "reject_reason": frame.reject_reason,
        "conid": frame.conid,
    }


def _decimal_str(value: Any) -> str | None:
    if value is None:
        return None
    return format(value, "f") if hasattr(value, "as_tuple") else str(value)


def get_orders_broadcaster() -> OrdersBroadcaster:
    raise RuntimeError(
        "get_orders_broadcaster muss in der App per dependency_overrides "
        "gesetzt werden"
    )


__all__ = [
    "OrderStreamEvent",
    "OrdersBroadcaster",
    "get_orders_broadcaster",
]
