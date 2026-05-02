"""Tests fuer ``broker_gateway.streams.registry.SubscriptionRegistry``.

Pruefen:

1. ``add()`` registriert und liefert den Refcount.
2. Doppeltes ``add(same_owner)`` ist idempotent (Refcount bleibt 1).
3. Doppeltes ``add(different_owner)`` zaehlt zwei.
4. ``remove()`` reduziert; bei 0 wird der Eintrag geloescht.
5. ``replay()`` ruft den injizierten Subscribe-Callable fuer jeden Eintrag
   genau einmal auf.
6. ``pause()`` blockiert ``replay()``, ``resume()`` hebt das auf.
7. Subscribe-Fehler im Replay werden geschluckt (Logger), nicht
   propagiert - ein Reconnect-Hook darf nicht abreissen.
8. ``count()`` reflektiert add/remove fuer den ``/v1/status``-Endpoint
   (AP-11 K8).
9. Lookup-Schluessel beruecksichtigt ``args``-Werte: gleiche topic mit
   verschiedenen ``conid`` sind verschiedene Eintraege.
10. ``CPWebSocketClient.add_on_connected_callback`` triggert ``replay()``
    nach Connect und nach Reconnect.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

import pytest

from broker_gateway.streams.registry import SubscriptionRegistry


class _Recorder:
    """Sammelt Subscribe-Calls fuer den Test."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_for_topics: set[str] = set()

    async def __call__(self, topic: str, args: dict[str, Any]) -> None:
        self.calls.append((topic, dict(args)))
        if topic in self.fail_for_topics:
            raise RuntimeError(f"forced failure for {topic}")


# ---------------------------------------------------------------------------
# 1./2. add() inkrementiert, gleiche owner ist idempotent
# ---------------------------------------------------------------------------


async def test_add_returns_refcount_starting_at_one() -> None:
    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)

    owner = object()
    refcount = await registry.add("smd", {"conid": 265598}, owner)

    assert refcount == 1


async def test_add_with_same_owner_is_idempotent() -> None:
    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)

    owner = object()
    first = await registry.add("smd", {"conid": 265598}, owner)
    second = await registry.add("smd", {"conid": 265598}, owner)

    assert first == 1
    assert second == 1


# ---------------------------------------------------------------------------
# 3./4. zwei Owner zaehlen zwei; remove erst bei 0 entfernen
# ---------------------------------------------------------------------------


async def test_add_with_different_owners_increments_refcount() -> None:
    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)

    a = object()
    b = object()
    await registry.add("smd", {"conid": 265598}, a)
    refcount = await registry.add("smd", {"conid": 265598}, b)

    assert refcount == 2


async def test_remove_decrements_and_removes_at_zero() -> None:
    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)

    a = object()
    b = object()
    await registry.add("smd", {"conid": 265598}, a)
    await registry.add("smd", {"conid": 265598}, b)

    after_first = await registry.remove("smd", {"conid": 265598}, a)
    after_second = await registry.remove("smd", {"conid": 265598}, b)

    assert after_first == 1
    assert after_second == 0
    assert registry.count() == 0


async def test_remove_unknown_owner_is_idempotent() -> None:
    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)

    owner = object()
    other = object()
    await registry.add("smd", {"conid": 265598}, owner)

    # ``other`` hat nie ``add`` gerufen - das soll keinen Fehler werfen.
    refcount = await registry.remove("smd", {"conid": 265598}, other)
    assert refcount == 1

    # Auch ein remove() auf einen voellig unbekannten Schluessel ist OK.
    refcount2 = await registry.remove("smd", {"conid": 0}, owner)
    assert refcount2 == 0


# ---------------------------------------------------------------------------
# 5. replay() ruft subscribe fuer jeden aktiven Eintrag
# ---------------------------------------------------------------------------


async def test_replay_calls_subscribe_for_each_active_entry() -> None:
    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)

    owner = object()
    await registry.add("smd", {"conid": 265598}, owner)
    await registry.add("smd", {"conid": 272093}, owner)
    await registry.add("sor", {"account": "U25235077"}, owner)

    sent = await registry.replay()

    assert sent == 3
    topics = [topic for topic, _args in rec.calls]
    assert sorted(topics) == ["smd", "smd", "sor"]
    args_for_smd = sorted(
        args["conid"] for topic, args in rec.calls if topic == "smd"
    )
    assert args_for_smd == [265598, 272093]


# ---------------------------------------------------------------------------
# 6. pause/resume
# ---------------------------------------------------------------------------


async def test_pause_blocks_replay_and_resume_releases_it() -> None:
    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)

    owner = object()
    await registry.add("smd", {"conid": 265598}, owner)

    registry.pause()
    sent_paused = await registry.replay()
    assert sent_paused == 0
    assert rec.calls == []  # waehrend paused kein Call

    registry.resume()
    sent_resumed = await registry.replay()
    assert sent_resumed == 1
    assert rec.calls == [("smd", {"conid": 265598})]


# ---------------------------------------------------------------------------
# 7. Subscribe-Fehler im Replay werden geschluckt
# ---------------------------------------------------------------------------


async def test_replay_swallows_subscribe_failures() -> None:
    rec = _Recorder()
    rec.fail_for_topics.add("smd")
    registry = SubscriptionRegistry(subscribe=rec)

    owner = object()
    await registry.add("smd", {"conid": 265598}, owner)
    await registry.add("sor", {"account": "U25235077"}, owner)

    # Darf nicht raisen, obwohl smd-subscribe RuntimeError wirft.
    sent = await registry.replay()

    # smd schlug fehl, sor war erfolgreich - sent zaehlt nur erfolgreiche.
    assert sent == 1
    # Beide Calls wurden trotzdem versucht (subscribe wird VOR dem Throw
    # in calls[] eingetragen).
    assert len(rec.calls) == 2


# ---------------------------------------------------------------------------
# 8. count() fuer Status-Endpoint
# ---------------------------------------------------------------------------


async def test_count_reflects_active_keys() -> None:
    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)

    owner = object()
    assert registry.count() == 0
    await registry.add("smd", {"conid": 1}, owner)
    assert registry.count() == 1
    await registry.add("smd", {"conid": 2}, owner)
    assert registry.count() == 2
    # zweiter Owner auf bestehendem Schluessel = kein neuer Eintrag.
    await registry.add("smd", {"conid": 1}, object())
    assert registry.count() == 2
    await registry.remove("smd", {"conid": 2}, owner)
    assert registry.count() == 1


# ---------------------------------------------------------------------------
# 9. Lookup-Schluessel beruecksichtigt args-Werte
# ---------------------------------------------------------------------------


async def test_different_args_are_separate_entries() -> None:
    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)

    owner = object()
    await registry.add("smd", {"conid": 1}, owner)
    await registry.add("smd", {"conid": 2}, owner)

    assert registry.count() == 2

    sent = await registry.replay()
    assert sent == 2
    conids = sorted(args["conid"] for _topic, args in rec.calls)
    assert conids == [1, 2]


async def test_args_with_list_values_are_normalised_to_hashable_keys() -> None:
    """Hashable-Schluessel auch wenn args[*] = list (Listen werden zu
    Tuples konvertiert, sodass das ``frozenset`` aufgebaut werden kann)."""
    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)

    owner = object()
    refcount = await registry.add(
        "smd",
        {"conid": 1, "fields": ["31", "84", "86"]},
        owner,
    )
    assert refcount == 1

    # Gleiche Liste als zweite Subscription: idempotent fuer denselben Owner.
    refcount2 = await registry.add(
        "smd",
        {"conid": 1, "fields": ["31", "84", "86"]},
        owner,
    )
    assert refcount2 == 1


# ---------------------------------------------------------------------------
# 10. CPWebSocketClient.add_on_connected_callback triggert replay
# ---------------------------------------------------------------------------


class _FakeWSConnection:
    """Minimaler asyncio-WS-Mock, der das Auth-Ack sofort sendet."""

    def __init__(self, *, fail_first_recv: bool = False) -> None:
        self._auth_sent = False
        self._closed = False
        self._fail_first_recv = fail_first_recv
        self._sent: list[str] = []

    async def send(self, message: str) -> None:
        self._sent.append(message)

    async def recv(self) -> str:
        if not self._auth_sent:
            self._auth_sent = True
            return (
                '{"topic":"sts","args":{"connected":true,'
                '"authenticated":true,"competing":false}}'
            )
        if self._fail_first_recv:
            self._fail_first_recv = False
            raise RuntimeError("forced first recv failure")
        # Halte den Reader-Loop an: wir wollen keine weiteren Frames
        # verarbeiten, der Test interessiert sich nur fuer den Hook.
        await asyncio.sleep(3600)
        return ""  # never reached

    async def close(self) -> None:
        self._closed = True


async def test_add_on_connected_callback_fires_after_connect() -> None:
    from broker_gateway.cp.ws_client import CPWebSocketClient

    client = CPWebSocketClient(
        url="ws://test/ws",
        max_reconnect_attempts=0,
        connect_factory=lambda url, cookies: _async_return(_FakeWSConnection()),
        sleep=lambda s: _async_return(None),
    )

    rec = _Recorder()
    registry = SubscriptionRegistry(subscribe=rec)
    owner = object()
    await registry.add("smd", {"conid": 265598}, owner)
    client.add_on_connected_callback(registry.replay)

    try:
        await client.connect("session-id-12345")
        # Bei connect() wurde replay() aufgerufen -> ein Subscribe-Call.
        assert rec.calls == [("smd", {"conid": 265598})]
    finally:
        await client.aclose()


async def test_callback_failure_does_not_break_connect() -> None:
    """Ein fehlerhafter Callback darf den Client nicht in einen
    halben Zustand bringen."""
    from broker_gateway.cp.ws_client import CPWebSocketClient

    client = CPWebSocketClient(
        url="ws://test/ws",
        max_reconnect_attempts=0,
        connect_factory=lambda url, cookies: _async_return(_FakeWSConnection()),
        sleep=lambda s: _async_return(None),
    )

    async def boom() -> None:
        raise RuntimeError("callback boom")

    client.add_on_connected_callback(boom)

    try:
        await client.connect("session-id-12345")  # darf nicht raisen
        assert client.connected is True
    finally:
        await client.aclose()


async def _async_return(value: Any) -> Any:
    return value
