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
# Cool-Down analog zum Quotes-Pfad (streams/manager.py, Karte f43a1514). Faellt
# der letzte Consumer weg (refcount==0), wird die _AccountSubscription NICHT
# sofort verworfen, sondern erst nach dieser Frist abgeraeumt. Ein Reconnect
# binnen Frist cancelt den Teardown und findet Ringpuffer UND _next_event_id
# intakt, sodass der Last-Event-ID-Replay ueber einen kurzen Disconnect hinweg
# traegt. Deckt Netzwerk-Blips - kein durabler Replay ueber lange Disconnects
# (der Konsument reconciled dann via REST, siehe docs/api/v1.md Section 9.2).
_DEFAULT_COOL_DOWN_S = 5.0
# Obergrenze fuer den Dedup-Merker je Account (Karte 736c49a5). Eine
# Dauerverbindung (trading_robot, durch den 15-s-Heartbeat am Leben gehalten)
# haelt refcount>=1, die Subscription wird nie gedroppt - ohne Cap waechst der
# Merker monoton mit jeder je gesehenen order_id (auch laengst terminale).
# Als LRU gefuehrt: nur die aeltesten, laengst ruhenden (praktisch terminalen)
# Orders altern heraus; aktive Orders werden bei jedem Frame aufgefrischt und
# nie evakuiert. Grosszuegig ueber jeder realistischen Zahl gleichzeitig
# aktiver Orders.
_DEDUP_CACHE_SIZE = 1024


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
    def __init__(self, broadcaster: "OrdersBroadcaster", account: str) -> None:
        self._broadcaster = broadcaster
        self.account = account
        self._next_event_id = 0
        self.consumers: dict[str, asyncio.Queue[OrderStreamEvent | None]] = {}
        self.event_buffer: collections.deque[OrderStreamEvent] = (
            collections.deque(maxlen=_REPLAY_BUFFER_SIZE)
        )
        # Letzter gesendeter "order"-Payload je order_id (ohne last_event_at)
        # fuer die Dedup (Karte 736c49a5). LRU-geordnet und auf
        # _DEDUP_CACHE_SIZE gedeckelt, damit eine Dauerverbindung den Merker
        # nicht unbegrenzt wachsen laesst.
        self._last_order_key: collections.OrderedDict[Any, dict[str, Any]] = (
            collections.OrderedDict()
        )
        # Teardown-Aufschub bei refcount==0 (Cool-Down, Karte f43a1514).
        self._cool_down_task: asyncio.Task[None] | None = None

    @property
    def refcount(self) -> int:
        return len(self.consumers)

    def attach(
        self,
        consumer_id: str,
        queue: asyncio.Queue[OrderStreamEvent | None],
    ) -> None:
        # Ein (Re-)Connect cancelt einen laufenden Cool-Down: die Subscription
        # bleibt mit intaktem Ringpuffer und Event-Zaehler warm.
        self._cancel_cool_down_locked()
        self.consumers[consumer_id] = queue

    def detach(self, consumer_id: str) -> None:
        self.consumers.pop(consumer_id, None)
        if self.refcount == 0:
            # Nicht sofort abraeumen: Cool-Down-Frist starten, damit ein
            # schneller Reconnect den Replay-Zustand wiederfindet. Erst der
            # Ablauf ruft _teardown (analog streams/manager.py).
            self._cool_down_task = asyncio.create_task(
                self._cool_down_then_teardown(),
                name=f"orders-cool-down-{self.account}",
            )

    def _cancel_cool_down_locked(self) -> None:
        task = self._cool_down_task
        if task is not None and not task.done():
            task.cancel()
        self._cool_down_task = None

    async def _cool_down_then_teardown(self) -> None:
        try:
            await asyncio.sleep(self._broadcaster.cool_down_s)
        except asyncio.CancelledError:
            return
        if self.refcount > 0:
            return
        await self._broadcaster._teardown(self)

    def is_duplicate_order(self, payload: dict[str, Any]) -> bool:
        """True, wenn dieses ``order``-Frame semantisch identisch zum zuletzt
        fuer dieselbe ``order_id`` gesendeten ist; aktualisiert dabei den Merker.

        Die drei ib_async-Callbacks (openOrderEvent/orderStatusEvent/
        execDetailsEvent) feuern fuer denselben Trade-Zustand und liefern
        byte-identische Frames inklusive ``last_event_at`` - der Zeitstempel
        taugt darum nicht als Unterscheidung und wird aus dem Vergleich
        ausgenommen. Ein reines Zeitstempel-Update traegt fuer den Konsumenten
        ohnehin nichts Neues.
        """
        order_id = payload.get("order_id")
        key = {k: v for k, v in payload.items() if k != "last_event_at"}
        if self._last_order_key.get(order_id) == key:
            # Aktive Order: warm halten (ans Ende der LRU), damit sie nicht
            # trotz laufender Updates evakuiert wird.
            self._last_order_key.move_to_end(order_id)
            return True
        self._last_order_key[order_id] = key
        self._last_order_key.move_to_end(order_id)
        # Aelteste (laengst ruhende, praktisch terminale) Orders evakuieren.
        # Eine spaeter doch wieder aktive, evakuierte Order erzeugt hoechstens
        # einen einzelnen re-emittierten Frame - unschaedlich.
        while len(self._last_order_key) > _DEDUP_CACHE_SIZE:
            self._last_order_key.popitem(last=False)
        return False

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
    def __init__(self, *, cool_down_s: float = _DEFAULT_COOL_DOWN_S) -> None:
        self._subs: dict[str, _AccountSubscription] = {}
        self._lock = asyncio.Lock()
        self.cool_down_s = cool_down_s

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
                sub = _AccountSubscription(self, account)
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
                    # Kein sofortiges pop mehr: detach startet bei refcount==0
                    # den Cool-Down; erst dessen Ablauf raeumt die Subscription
                    # via _teardown ab (Karte f43a1514).
                    sub.detach(consumer_id)

        return _iterator()

    def publish(self, account: str, frame: SorFrame) -> None:
        """Speist ein Live-Order-Update in den Fan-Out ein.

        Wenn fuer ``account`` keine Subscription aktiv ist, wird das
        Frame still verworfen - der WS-Server kann nach einem
        Unsubscribe noch kurz nachsenden.

        Dedup (Karte 736c49a5): ein Frame, das mit dem zuletzt fuer
        dieselbe order_id gesendeten identisch ist, wird nicht erneut
        fan-outed. Sonst verdoppelt ein Fill-zaehlender Konsument die
        Fills, weil die drei ib_async-Callbacks denselben Trade-Zustand
        publishen. Bootstrap-Frames laufen ueber einen anderen event_type
        und sind davon nicht betroffen.
        """
        sub = self._subs.get(account)
        if sub is None:
            return
        payload = _frame_to_payload(frame)
        if sub.is_duplicate_order(payload):
            return
        sub.publish(event_type="order", payload=payload)

    async def _teardown(self, sub: _AccountSubscription) -> None:
        """Raeumt eine ausgekuehlte Subscription ab (Cool-Down abgelaufen).

        Analog zu ``SubscriptionManager._teardown`` (streams/manager.py):
        refcount wird unter dem Lock erneut geprueft - ein Reconnect kann in
        der Zwischenzeit einen Consumer angehaengt haben - und nur genau diese
        Instanz aus ``_subs`` genommen (Identitaets-Check gegen einen
        zwischenzeitlichen Ersatz). Es gibt keine externe Ressource wie beim
        Quotes-Pfad (kein Poll-Task, kein CP-Unsubscribe); der Teardown ist
        allein das Vergessen des Replay-Zustands.
        """
        async with self._lock:
            if sub.refcount > 0:
                return
            if self._subs.get(sub.account) is sub:
                self._subs.pop(sub.account, None)

    async def shutdown(self) -> None:
        """Cancelt alle offenen Cool-Down-Tasks und leert die Registry.

        Wird im App-Lifespan-Shutdown gerufen (analog
        ``SubscriptionManager.shutdown``), damit kein Cool-Down-Task den
        Prozess-Exit ueberdauert.
        """
        async with self._lock:
            subs = list(self._subs.values())
            self._subs.clear()
        for sub in subs:
            sub._cancel_cool_down_locked()

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
        "raw_status": frame.raw_status,
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
