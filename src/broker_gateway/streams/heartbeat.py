"""Cancel-sicherer SSE-Heartbeat-Wrapper (Karte 9b1d76ba).

Die SSE-Endpunkte (``/v1/orders/stream`` und ``/v1/quotes/stream``)
halten eine Verbindung offen und liefern in stillen
Phasen - nachts, ausserhalb der Handelszeiten - stundenlang nichts. Ein
Konsument mit einem ueblichen Read-Timeout kann eine gesunde, aber stille
Verbindung nicht von einer toten unterscheiden und baut sie im Timeout-Takt
neu auf. Der SSE-Standard sieht dafuer den **Comment** vor (Zeile beginnt mit
``:``): ein SSE-konformer Parser verwirft ihn, er zaehlt nicht als Event und
vergibt keine ``id`` - aber er haelt die Leitung warm.

Warum nicht ``asyncio.wait_for(source.__anext__(), interval)``
--------------------------------------------------------------
Der naheliegende Ansatz - ein Timeout direkt um ``__anext__`` - ist hier
**nicht** cancel-sicher. Die realen Quell-Iteratoren
(``OrdersBroadcaster``/``SubscriptionManager`` ``subscribe``)
sind async generators mit einem ``try/finally``, das den Consumer bei
Beendigung aus dem Fan-Out austraegt (``detach``). Ein ``wait_for``-Timeout
cancelt den **gesamten** Generator-Frame, nicht nur das ``await queue.get()``
darin - das injiziert ``CancelledError`` an der Suspendierung, laesst das
``finally`` laufen und der Consumer waere schon nach dem **ersten** Heartbeat
abgehaengt. Der Heartbeat wuerde genau das zerstoeren, was er retten soll.

Loesung
-------
:func:`sse_with_heartbeat` haelt das ``__anext__``-Future ueber die
Timeout-Grenze hinweg am Leben (es wird bei Timeout **nicht** gecancelt),
sendet nur einen Comment und wartet weiter auf dasselbe Future - ein direkt
nach dem Heartbeat eintreffendes Event geht dadurch nicht verloren. Erst beim
tatsaechlichen Ende (Client-Disconnect, ``aclose``) wird das Future gecancelt
und der Quell-Iterator ueber ``aclose`` sauber geschlossen, sodass dessen
``detach``-``finally`` garantiert und ohne GC-Verzoegerung laeuft.

Der Wrapper ist generisch: er nimmt einen beliebigen Async-Iterator plus eine
``render``-Funktion (Event -> SSE-Bytes) und ist damit fuer beide
Endpunkte identisch verwendbar.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import TypeVar

T = TypeVar("T")

# Der zugesagte Vertrag (Docstring orders_stream.py, ws-adapter-design.md:241,
# docs/api/v1.md): 15-s-Intervall, SSE-Comment ``: keepalive``.
DEFAULT_HEARTBEAT_INTERVAL_S = 15.0
DEFAULT_HEARTBEAT_COMMENT = b": keepalive\n\n"

# Sentinel fuer "Quelle erschoepft" - so muss StopAsyncIteration nicht durch
# ein Future/Task getragen werden (asyncio behandelt StopIteration-Familie in
# Tasks gesondert; ein Sentinel ist eindeutig und robust).
_EXHAUSTED = object()


async def sse_with_heartbeat(
    source: AsyncIterator[T],
    render: Callable[[T], bytes],
    *,
    interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    comment: bytes = DEFAULT_HEARTBEAT_COMMENT,
) -> AsyncIterator[bytes]:
    """Umkreist ``source`` mit einem Keepalive-Comment bei Stille.

    Liefert die von ``render`` erzeugten SSE-Bytes pro Event; vergeht
    ``interval_s`` ohne Event, wird stattdessen ``comment`` gesendet und weiter
    auf dasselbe Event gewartet (kein Event-Verlust). Bei Beendigung
    (Erschoepfung der Quelle, ``aclose`` durch den ASGI-Server bei
    Client-Disconnect) wird die Quelle sauber geschlossen.
    """
    iterator = source.__aiter__()
    pending: asyncio.Task[object] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(_next_or_exhausted(iterator))
            done, _ = await asyncio.wait({pending}, timeout=interval_s)
            if not done:
                # Stille: Leitung warmhalten. Das pending-Future bleibt
                # unangetastet, ein gleich eintreffendes Event ueberlebt.
                yield comment
                continue
            item = pending.result()
            pending = None
            if item is _EXHAUSTED:
                return
            yield render(item)  # type: ignore[arg-type]
    finally:
        # Ein noch laufendes __anext__ beenden - das injiziert CancelledError
        # an dessen await queue.get() und loest damit das detach-finally der
        # Quelle aus.
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending
        # Direktes aclose der Quelle: garantiert das detach-finally auch dann,
        # wenn zuletzt an einem Event-yield (pending bereits None) gestanden
        # wurde - ohne auf den GC zu warten.
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


async def _next_or_exhausted(iterator: AsyncIterator[T]) -> object:
    """``await iterator.__anext__()``, aber mit Sentinel statt
    StopAsyncIteration bei erschoepfter Quelle."""
    try:
        return await iterator.__anext__()
    except StopAsyncIteration:
        return _EXHAUSTED


__all__ = [
    "DEFAULT_HEARTBEAT_COMMENT",
    "DEFAULT_HEARTBEAT_INTERVAL_S",
    "sse_with_heartbeat",
]
