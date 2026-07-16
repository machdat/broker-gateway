"""Tests fuer den cancel-sicheren SSE-Heartbeat-Wrapper (Karte 9b1d76ba).

Der Wrapper umkreist einen Quell-Async-Iterator (in Produktion die queue-
basierten Broadcaster/Manager-``subscribe``-Iteratoren mit ihrem
``try/finally: detach``) mit einem Keepalive-Comment bei Stille. Die Tests
verifizieren ihn ISOLIERT mit einem Fake-Source, der die reale Struktur
spiegelt (echter async generator, queue-basiert, finally-Cleanup) - Live-SSE-
Konsum ueber den echten Endpunkt haengt im pytest-Event-Loop (siehe
test_orders_stream.py / test_quotes_stream.py) und ist hier bewusst vermieden.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import AsyncIterator

import pytest

from broker_gateway.streams.heartbeat import (
    DEFAULT_HEARTBEAT_COMMENT,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    sse_with_heartbeat,
)

# Sentinel: signalisiert dem Fake-Source ein sauberes Stream-Ende.
_STOP = object()


def _render(event: str) -> bytes:
    return f"R:{event}".encode("utf-8")


def _queue_source(
    queue: "asyncio.Queue[object]",
    *,
    on_close: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    """Echter async generator wie die realen Stream-Iteratoren: queue-basiert
    mit ``try/finally``-Cleanup (detach-Simulation ueber ``on_close``)."""

    async def _gen() -> AsyncIterator[str]:
        try:
            while True:
                item = await queue.get()
                if item is _STOP:
                    return
                yield item  # type: ignore[misc]
        finally:
            if on_close is not None:
                on_close()

    return _gen()


async def test_heartbeat_bei_stille_sendet_comment() -> None:
    """Bleibt die Quelle stumm, sendet der Wrapper alle ``interval_s`` einen
    Comment."""
    queue: asyncio.Queue[object] = asyncio.Queue()  # bleibt leer
    gen = sse_with_heartbeat(
        _queue_source(queue), _render, interval_s=0.02, comment=b": beat\n\n"
    )
    beats = [await gen.__anext__() for _ in range(3)]
    await gen.aclose()
    assert beats == [b": beat\n\n", b": beat\n\n", b": beat\n\n"]


async def test_event_wird_gerendert_durchgereicht() -> None:
    """Ein sofort verfuegbares Event wird via render() durchgereicht, ohne
    vorher einen Heartbeat zu senden."""
    queue: asyncio.Queue[object] = asyncio.Queue()
    queue.put_nowait("EV1")
    gen = sse_with_heartbeat(_queue_source(queue), _render, interval_s=5.0)
    first = await gen.__anext__()
    await gen.aclose()
    assert first == b"R:EV1"


async def test_source_ueberlebt_heartbeat_und_liefert_weiter() -> None:
    """KERN DER CANCEL-SICHERHEIT: ein Heartbeat bei Stille darf den Quell-
    Iterator NICHT beenden (kein detach), und ein danach eintreffendes Event
    darf nicht verloren gehen.

    Der naive Ansatz der Karte - asyncio.wait_for(source.__anext__(), 15s) -
    wuerde bei Timeout das __anext__ canceln, dessen finally (detach) ausloesen
    und die Quelle toeten: hier wuerde closed schon nach dem ersten Heartbeat
    [True] sein und das Folge-Event ginge verloren (StopAsyncIteration)."""
    queue: asyncio.Queue[object] = asyncio.Queue()
    closed: list[bool] = []
    gen = sse_with_heartbeat(
        _queue_source(queue, on_close=lambda: closed.append(True)),
        _render,
        interval_s=0.02,
        comment=b": beat\n\n",
    )
    # Zwei Heartbeats bei Stille.
    assert await gen.__anext__() == b": beat\n\n"
    assert await gen.__anext__() == b": beat\n\n"
    # Quelle darf durch die Heartbeats NICHT geschlossen worden sein.
    assert closed == []
    # Und sie liefert danach weiter - das Event geht nicht verloren.
    queue.put_nowait("STILL_ALIVE")
    assert await gen.__anext__() == b"R:STILL_ALIVE"
    await gen.aclose()
    assert closed == [True]  # erst der aclose loest das detach aus


async def test_kein_event_verlust_direkt_nach_heartbeat() -> None:
    """Ein Event, das genau nach einem Heartbeat-Timeout eintrifft, wird
    zugestellt - das ueber den Timeout gehaltene __anext__-Future faengt es."""
    queue: asyncio.Queue[object] = asyncio.Queue()
    gen = sse_with_heartbeat(
        _queue_source(queue), _render, interval_s=0.02, comment=b": beat\n\n"
    )
    assert await gen.__anext__() == b": beat\n\n"  # Stille -> Heartbeat
    queue.put_nowait("EV2")  # Event NACH dem Heartbeat
    assert await gen.__anext__() == b"R:EV2"
    await gen.aclose()


async def test_cleanup_schliesst_source_bei_aclose() -> None:
    """Client-Disconnect (aclose auf dem Wrapper) schliesst den Quell-Iterator
    -> dessen finally (detach) laeuft garantiert, ohne GC-Verzoegerung."""
    queue: asyncio.Queue[object] = asyncio.Queue()  # leer -> haengt an get()
    closed: list[bool] = []
    gen = sse_with_heartbeat(
        _queue_source(queue, on_close=lambda: closed.append(True)),
        _render,
        interval_s=0.02,
    )
    await gen.__anext__()  # ein Heartbeat, damit der Stream laeuft
    assert closed == []
    await gen.aclose()
    assert closed == [True]


async def test_cleanup_nach_event_schliesst_source() -> None:
    """Auch wenn der Wrapper zuletzt an einem Event-yield stand (pending
    bereits konsumiert), schliesst aclose den Quell-Iterator."""
    queue: asyncio.Queue[object] = asyncio.Queue()
    closed: list[bool] = []
    queue.put_nowait("EV")
    gen = sse_with_heartbeat(
        _queue_source(queue, on_close=lambda: closed.append(True)),
        _render,
        interval_s=5.0,
    )
    assert await gen.__anext__() == b"R:EV"
    assert closed == []
    await gen.aclose()
    assert closed == [True]


async def test_stop_async_iteration_beendet_sauber() -> None:
    """Ein sauberes Stream-Ende der Quelle beendet den Wrapper mit
    StopAsyncIteration und laeuft durch das finally (detach)."""
    queue: asyncio.Queue[object] = asyncio.Queue()
    closed: list[bool] = []
    queue.put_nowait("EV3")
    queue.put_nowait(_STOP)
    gen = sse_with_heartbeat(
        _queue_source(queue, on_close=lambda: closed.append(True)),
        _render,
        interval_s=5.0,
    )
    assert await gen.__anext__() == b"R:EV3"
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert closed == [True]


async def test_render_nur_auf_events_nicht_auf_heartbeat() -> None:
    """Der Heartbeat ist der rohe Comment - render wird darauf NICHT
    angewandt (sonst waere er kein SSE-Comment mehr)."""
    queue: asyncio.Queue[object] = asyncio.Queue()
    calls: list[str] = []

    def render_spy(event: str) -> bytes:
        calls.append(event)
        return _render(event)

    gen = sse_with_heartbeat(
        _queue_source(queue), render_spy, interval_s=0.02, comment=b": beat\n\n"
    )
    assert await gen.__anext__() == b": beat\n\n"
    assert calls == []  # render nie fuer einen Heartbeat aufgerufen
    await gen.aclose()


async def test_heartbeat_default_ist_sse_comment_ohne_id() -> None:
    """Der Default-Comment ist ein SSE-Comment (beginnt mit ':'), traegt keine
    event-id und ist kein data-Frame - sonst verschoebe er die Last-Event-ID
    oder wuerde als Event fehlinterpretiert."""
    assert DEFAULT_HEARTBEAT_COMMENT.startswith(b":")
    assert b"id:" not in DEFAULT_HEARTBEAT_COMMENT
    assert b"event:" not in DEFAULT_HEARTBEAT_COMMENT
    assert b"data:" not in DEFAULT_HEARTBEAT_COMMENT
    assert DEFAULT_HEARTBEAT_COMMENT.endswith(b"\n\n")


async def test_defaults_15s_und_keepalive() -> None:
    """Der zugesagte Vertrag: 15s-Intervall, ': keepalive'-Comment."""
    assert DEFAULT_HEARTBEAT_INTERVAL_S == 15.0
    assert DEFAULT_HEARTBEAT_COMMENT == b": keepalive\n\n"


async def test_echte_quell_exception_propagiert_und_cleanup() -> None:
    """Wirft die Quelle einen ECHTEN Fehler (nicht StopAsyncIteration),
    propagiert er zum Konsumenten - und das detach-finally laeuft trotzdem.

    Haertet jemand `_next_or_exhausted` faelschlich zu `except Exception:
    return _EXHAUSTED`, wuerde der Fehler still zu einem sauberen Stream-Ende
    (der Konsument saehe ein normales EOF, reconnectet, der Fehler verschwaende
    spurlos). Genau das faengt dieser Test."""
    closed: list[bool] = []

    async def _boom() -> AsyncIterator[str]:
        try:
            raise RuntimeError("source boom")
        finally:
            closed.append(True)
        yield "unreached"  # macht die Fn zum async generator (unreachable)

    gen = sse_with_heartbeat(_boom(), _render, interval_s=5.0)
    with pytest.raises(RuntimeError, match="source boom"):
        await gen.__anext__()
    assert closed == [True]


async def test_aclose_fehler_im_cleanup_wird_geloggt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scheitert das detach-finally der Quelle beim aclose (z.B. ein kuenftiger
    WS-Unsubscribe), wird der Cleanup nicht abgebrochen, aber der Fehler
    sichtbar geloggt - Konvention wie die Schwester-Stream-Module."""
    queue: asyncio.Queue[object] = asyncio.Queue()
    queue.put_nowait("EV")

    async def _gen() -> AsyncIterator[str]:
        try:
            while True:
                item = await queue.get()
                if item is _STOP:
                    return
                yield item  # type: ignore[misc]
        finally:
            raise RuntimeError("detach boom")

    gen = sse_with_heartbeat(_gen(), _render, interval_s=5.0)
    assert await gen.__anext__() == b"R:EV"  # Event geliefert -> pending jetzt None
    with caplog.at_level(
        logging.WARNING, logger="broker_gateway.streams.heartbeat"
    ):
        await gen.aclose()  # aclose triggert das werfende detach-finally
    assert any("aclose" in r.getMessage().lower() for r in caplog.records), (
        f"kein aclose-Fehler-Log: {[r.getMessage() for r in caplog.records]}"
    )


async def test_task_cancel_propagiert_und_detacht() -> None:
    """Ein Cancel des Wrapper-Tasks (der echte uvicorn-Disconnect-Pfad, nicht
    nur aclose) propagiert sauber und traegt den Consumer aus (detach). Belegt
    die Cancel-Sicherheit im Task-Kontext - die committete Suite testete
    bisher nur den aclose-Zwilling."""
    queue: asyncio.Queue[object] = asyncio.Queue()  # bleibt leer
    closed: list[bool] = []

    async def _drive() -> None:
        gen = sse_with_heartbeat(
            _queue_source(queue, on_close=lambda: closed.append(True)),
            _render,
            interval_s=0.02,
        )
        async for _ in gen:  # verwirft die Heartbeats
            pass

    task = asyncio.ensure_future(_drive())
    await asyncio.sleep(0.05)  # gen laeuft, sendet Heartbeats
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True]  # der Cancel hat das detach ausgeloest


async def test_iterator_ohne_aclose_laeuft_und_endet() -> None:
    """Eine Quelle ohne `aclose` (kein async generator) wird korrekt konsumiert
    und der Cleanup ueberspringt den aclose-Schritt ohne Absturz - der
    `getattr(..., None) is None`-Zweig ist damit abgedeckt."""

    class _NoAclose:
        def __init__(self) -> None:
            self._items = iter(["A", "B"])

        def __aiter__(self) -> "_NoAclose":
            return self

        async def __anext__(self) -> str:
            try:
                return next(self._items)
            except StopIteration:
                raise StopAsyncIteration from None

    gen = sse_with_heartbeat(_NoAclose(), _render, interval_s=5.0)
    assert await gen.__anext__() == b"R:A"
    assert await gen.__anext__() == b"R:B"
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    await gen.aclose()  # getattr(iterator, "aclose", None) is None -> kein Absturz
