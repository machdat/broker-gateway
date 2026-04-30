"""Tests fuer src/broker_gateway/cp/ws_client.py.

Strategie: ``CPWebSocketClient`` bekommt einen injizierten
``connect_factory``, der eine :class:`FakeConnection` liefert. Damit ist
kein echter WS-Server noetig - die Tests validieren das Verhalten gegen
deterministische Frame-Sequenzen, die u.a. aus der WS-Replay-Fixture
``tests/fixtures/recorded/ws/spike-baseline.jsonl`` (K2) stammen.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from broker_gateway.cp.ws_client import (
    CPWebSocketClient,
    WSAuthError,
    WSIncomingFrame,
)
from tests.cp_mock.ws_replay import iter_server_frames, load_ws_frames


BASELINE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "recorded"
    / "ws"
    / "spike-baseline.jsonl"
)


# ---- Fakes ---------------------------------------------------------------


@dataclass
class _RecvFail:
    exc: BaseException


class _RecvEnd:
    pass


_RECV_END = _RecvEnd()


class FakeConnection:
    """In-Memory-Stub, der das ``WSConnection``-Protocol erfuellt.

    ``feed(raw)`` legt einen Server-Frame in die Inbox; der naechste
    ``recv()``-Aufruf gibt ihn aus. ``feed_error(exc)`` provoziert beim
    naechsten ``recv()`` eine Exception (z.B. ``ConnectionError`` als
    broken-pipe-Stand-In).
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._inbox: asyncio.Queue[str | _RecvFail | _RecvEnd] = asyncio.Queue()

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionError("send nach close")
        self.sent.append(message)

    async def recv(self) -> str:
        item = await self._inbox.get()
        if isinstance(item, _RecvFail):
            raise item.exc
        if isinstance(item, _RecvEnd):
            raise ConnectionError("Inbox geschlossen")
        return item

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # Entblocke ein evtl. wartendes recv() durch Push eines End-Markers.
        await self._inbox.put(_RECV_END)

    # Test-Helfer

    def feed(self, raw: str) -> None:
        self._inbox.put_nowait(raw)

    def feed_error(self, exc: BaseException) -> None:
        self._inbox.put_nowait(_RecvFail(exc))


def _sts_auth_ack(authenticated: bool = True) -> str:
    return json.dumps(
        {
            "topic": "sts",
            "args": {
                "connected": True,
                "authenticated": authenticated,
                "established": authenticated,
                "competing": False,
                "message": "",
                "fail": "" if authenticated else "Auth lost",
            },
        }
    )


def _make_factory(connections: list[FakeConnection]):
    """Liefert eine connect_factory, die der Reihe nach die Connections
    aus der Liste ausgibt. Nach Erschoepfung wird ``ConnectionError``
    geworfen."""
    iterator = iter(connections)

    async def factory(url: str, cookies: str | None) -> FakeConnection:
        try:
            return next(iterator)
        except StopIteration as exc:
            raise ConnectionError("Keine FakeConnection mehr verfuegbar") from exc

    return factory


async def _drain_until(client: CPWebSocketClient, n: int) -> list[WSIncomingFrame]:
    """Lese bis zu ``n`` Frames vom Iterator. Liefert weniger zurueck,
    wenn der Iterator vorher endet."""
    out: list[WSIncomingFrame] = []
    aiter_ = client.__aiter__()
    for _ in range(n):
        try:
            frame = await asyncio.wait_for(aiter_.__anext__(), timeout=1.0)
        except StopAsyncIteration:
            break
        out.append(frame)
    return out


# ---- Tests ---------------------------------------------------------------


async def test_connect_auth_success_with_sts_authenticated_true() -> None:
    fake = FakeConnection()
    fake.feed(_sts_auth_ack(authenticated=True))

    sleeps: list[float] = []

    async def never_returning_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.Event().wait()  # Pinger blockiert -> kein zweites tic

    client = CPWebSocketClient(
        url="ws://test/v1/api/ws",
        connect_factory=_make_factory([fake]),
        sleep=never_returning_sleep,
    )

    await client.connect("sess-abc", cookies="x-sess-uuid=foo")
    assert client.connected is True
    # Auth-Frame muss als allererster Send rausgegangen sein.
    assert fake.sent[0] == json.dumps({"session": "sess-abc"})
    await client.aclose()


async def test_connect_auth_failure_when_sts_authenticated_false() -> None:
    fake = FakeConnection()
    fake.feed(_sts_auth_ack(authenticated=False))

    client = CPWebSocketClient(
        url="ws://test/v1/api/ws",
        connect_factory=_make_factory([fake]),
        sleep=asyncio.sleep,
    )
    with pytest.raises(WSAuthError, match="authenticated=false"):
        await client.connect("sess-abc")
    # Connection wurde geschlossen, Single-Owner-Flag verhindert Retry.
    assert fake.closed is True
    with pytest.raises(RuntimeError, match="nur einmal"):
        await client.connect("sess-abc")


async def test_connect_auth_timeout_when_no_sts_arrives() -> None:
    # Inbox bleibt leer - recv() haengt; wait_for schlaegt Timeout zu.
    fake = FakeConnection()

    client = CPWebSocketClient(
        url="ws://test/v1/api/ws",
        connect_factory=_make_factory([fake]),
        sleep=asyncio.sleep,
    )
    with pytest.raises(WSAuthError, match="Kein sts-Auth-Ack"):
        await client.connect("sess-abc", auth_timeout_s=0.05)


async def test_tic_ping_loop_sends_tic() -> None:
    fake = FakeConnection()
    fake.feed(_sts_auth_ack(authenticated=True))

    sleep_started = asyncio.Event()
    release = asyncio.Event()
    sleeps: list[float] = []

    async def gated_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 1:
            sleep_started.set()
            return  # sofort zurueck -> Pinger sendet einmal tic
        await release.wait()  # alle weiteren Sleep-Calls hangen

    client = CPWebSocketClient(
        url="ws://test/v1/api/ws",
        connect_factory=_make_factory([fake]),
        sleep=gated_sleep,
        ping_interval_s=42.0,
    )
    await client.connect("sess-abc")
    await asyncio.wait_for(sleep_started.wait(), timeout=1.0)
    # gib dem Pinger einen Tick, um nach dem Sleep tic zu senden
    for _ in range(50):
        if "tic" in fake.sent:
            break
        await asyncio.sleep(0.01)

    assert sleeps[0] == 42.0
    assert "tic" in fake.sent
    release.set()
    await client.aclose()


def _gated_sleep_factory():
    """Sleep, das nur ``seconds == 0`` direkt zurueckkehrt; alles andere
    blockiert bis ``release.set()``. Damit kann Reconnect-Backoff (0 s)
    weiterlaufen, der Pinger (ping_interval_s) bleibt aber haengen und
    erzeugt keinen Busy-Loop in Tests mit reconnect_backoff_s=0.0."""
    release = asyncio.Event()

    async def gated_sleep(seconds: float) -> None:
        if seconds == 0.0:
            return
        await release.wait()

    return gated_sleep, release


async def test_reconnect_on_broken_pipe_uses_fresh_connection() -> None:
    first = FakeConnection()
    first.feed(_sts_auth_ack(authenticated=True))
    first.feed_error(ConnectionError("broken pipe"))

    second = FakeConnection()
    second.feed(_sts_auth_ack(authenticated=True))
    second.feed(json.dumps({"topic": "system", "hb": 1}))

    gated_sleep, release = _gated_sleep_factory()

    client = CPWebSocketClient(
        url="ws://test/v1/api/ws",
        connect_factory=_make_factory([first, second]),
        sleep=gated_sleep,
        max_reconnect_attempts=3,
        reconnect_backoff_s=0.0,
    )
    await client.connect("sess-abc")

    frames = await _drain_until(client, 1)
    assert len(frames) == 1
    assert frames[0].topic == "system"
    assert frames[0].parsed == {"topic": "system", "hb": 1}
    # Reconnect hat den Auth-Frame an die zweite Connection geschickt -
    # das ist die ganze Pointe von "Reconnect mit neuem Auth-Flow".
    auth_frame = json.dumps({"session": "sess-abc"})
    assert auth_frame in second.sent
    release.set()
    await client.aclose()


async def test_reconnect_gives_up_after_max_attempts() -> None:
    first = FakeConnection()
    first.feed(_sts_auth_ack(authenticated=True))
    first.feed_error(ConnectionError("broken pipe"))

    # Folge-Connections schlagen sofort beim Auth-Wait fehl
    # (sts.authenticated=false -> WSAuthError).
    def make_failing() -> FakeConnection:
        c = FakeConnection()
        c.feed(_sts_auth_ack(authenticated=False))
        return c

    gated_sleep, release = _gated_sleep_factory()

    connections = [first, make_failing(), make_failing(), make_failing()]
    client = CPWebSocketClient(
        url="ws://test/v1/api/ws",
        connect_factory=_make_factory(connections),
        sleep=gated_sleep,
        max_reconnect_attempts=3,
        reconnect_backoff_s=0.0,
    )
    await client.connect("sess-abc")

    # Reader merkt broken pipe, alle 3 Reconnect-Versuche scheitern -
    # Iterator endet sauber mit StopAsyncIteration.
    frames = await _drain_until(client, 5)
    assert frames == []
    release.set()
    await client.aclose()


async def test_single_owner_double_connect_rejected() -> None:
    fake = FakeConnection()
    fake.feed(_sts_auth_ack(authenticated=True))

    async def never_sleep(seconds: float) -> None:
        await asyncio.Event().wait()

    client = CPWebSocketClient(
        url="ws://test/v1/api/ws",
        connect_factory=_make_factory([fake]),
        sleep=never_sleep,
    )
    await client.connect("sess-abc")
    with pytest.raises(RuntimeError, match="Single-Owner"):
        await client.connect("sess-abc")
    await client.aclose()


async def test_frame_iteration_yields_in_order_with_baseline_fixture() -> None:
    """Replay der spike-baseline-Fixture: Reader liefert Topics in
    derselben Reihenfolge wie das Recording."""
    recorded = list(iter_server_frames(load_ws_frames(BASELINE)))
    assert recorded[0].topic == "system"  # Sanity-Check der Fixture

    fake = FakeConnection()
    # Erster Frame ist der initiale system+success-Frame nach dem auth-send,
    # aber Auth-Ack ist erst sts. Die spike-baseline kommt in dieser Reihe:
    # system (success), act, sts, system (hb), system (hb), system (hb), tic*4 ...
    # Der Auth-Wait konsumiert die Frames bis zum sts. Der Iterator sieht
    # dann ab dem ersten Frame nach sts.
    for frame in recorded:
        fake.feed(frame.raw)

    async def never_sleep(seconds: float) -> None:
        await asyncio.Event().wait()

    client = CPWebSocketClient(
        url="ws://test/v1/api/ws",
        connect_factory=_make_factory([fake]),
        sleep=never_sleep,
    )
    await client.connect("BASELINE_USER-session")

    # Erwartete Topic-Sequenz nach sts (sts wird vom Auth-Wait konsumiert):
    # 3x system (hb), 8x tic (2 Pings * 4 Echos), insgesamt 11 Frames.
    expected_topics_after_sts = [
        f.topic for f in recorded
    ][3:]  # nach system(success) + act + sts
    frames = await _drain_until(client, len(expected_topics_after_sts))
    actual_topics = [f.topic for f in frames]
    assert actual_topics == expected_topics_after_sts
    await client.aclose()


async def test_send_before_connect_raises() -> None:
    client = CPWebSocketClient(
        url="ws://test/v1/api/ws",
        connect_factory=_make_factory([FakeConnection()]),
        sleep=asyncio.sleep,
    )
    with pytest.raises(RuntimeError, match="vor connect oder nach close"):
        await client.send("smd+265598+{}")


async def test_send_after_close_raises() -> None:
    fake = FakeConnection()
    fake.feed(_sts_auth_ack(authenticated=True))

    async def never_sleep(seconds: float) -> None:
        await asyncio.Event().wait()

    client = CPWebSocketClient(
        url="ws://test/v1/api/ws",
        connect_factory=_make_factory([fake]),
        sleep=never_sleep,
    )
    await client.connect("sess-abc")
    await client.aclose()
    with pytest.raises(RuntimeError, match="vor connect oder nach close"):
        await client.send("smd+265598+{}")
