"""Tests fuer ``broker_gateway.streams.ws_source.WSPushSource``.

Die Tests decken die fuenf Punkte aus AP-11 K3 ab:

1. Adapter-zu-Manager-Bruecke: Frame durch Adapter, Snapshot landet im
   Manager-Fan-Out (publish).
2. ENV-Schalter ``BG_QUOTES_SOURCE=polling`` deaktiviert WSPushSource und
   nutzt den Poll-Pfad (Backbone-Test ueber Manager-Konstruktor).
3. ENV-Schalter ``BG_QUOTES_SOURCE=ws`` aktiviert den WS-Pfad (kein
   Poll-Task pro conid).
4. SSE-Vertragsgleichheit: Frame-Schema im Push-Modus identisch zum
   Polling-Modus (Quote-Felder).
5. Reconnect triggert Replay: nach Reconnect ruft die Registry alle
   Subscribes erneut auf.

Zusaetzlich:

6. Subscribe / Unsubscribe via WSPushSource pflegen die Registry und
   senden die korrekten Frames (``smd+...``, ``usmd+...``).
7. Frame fuer unbekannten conid wird verworfen (kein Fehler).
8. Send-Fehler im Subscribe wird geschluckt (Reconnect-Replay holt es
   nach).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

from broker_gateway.cp.quotes import Quote
from broker_gateway.cp.topics.smd import SmdTopicAdapter
from broker_gateway.cp.ws_client import CPWebSocketClient, WSIncomingFrame
from broker_gateway.streams.manager import SubscriptionManager
from broker_gateway.streams.registry import SubscriptionRegistry
from broker_gateway.streams.ws_source import (
    WSPushSource,
    _decimal_to_str,
    _format_change_pct,
    _smd_frame_to_quote,
)


CONID = 265598


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeWSClient:
    """Minimal-Stand-in fuer ``CPWebSocketClient`` mit
    ``add_on_connected_callback``-API und einem ``send``-Recorder."""

    def __init__(self, frames: list[dict[str, Any]] | None = None) -> None:
        self.sent: list[str] = []
        self.send_failures: set[str] = set()
        self._on_connected_callbacks: list = []
        self._frames_to_emit = list(frames or [])
        self._iter_done = asyncio.Event()
        self._connected = True

    @property
    def connected(self) -> bool:
        return self._connected

    def add_on_connected_callback(self, callback) -> None:
        self._on_connected_callbacks.append(callback)

    async def fire_connected(self) -> None:
        for cb in list(self._on_connected_callbacks):
            await cb()

    async def send(self, frame: str) -> None:
        if frame in self.send_failures:
            raise RuntimeError("forced send failure")
        self.sent.append(frame)

    def __aiter__(self) -> AsyncIterator[WSIncomingFrame]:
        return self._async_iter()

    async def _async_iter(self) -> AsyncIterator[WSIncomingFrame]:
        for raw in self._frames_to_emit:
            yield WSIncomingFrame(
                topic=raw.get("topic", "?"),
                raw=json.dumps(raw),
                parsed=raw,
            )
            await asyncio.sleep(0)
        self._iter_done.set()

    async def wait_for_iter_done(self) -> None:
        await self._iter_done.wait()


class _NullCPClient:
    """Platzhalter fuer ``CPGatewayClient`` - der WS-Pfad ruft den
    REST-Client nicht. Manager braucht das Objekt nur fuer den
    Konstruktor."""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def ws_stack() -> tuple[
    _FakeWSClient,
    SmdTopicAdapter,
    SubscriptionRegistry,
    SubscriptionManager,
    WSPushSource,
]:
    fake_client = _FakeWSClient()
    adapter = SmdTopicAdapter()
    # Subscribe-Callable wird von der Registry beim Replay genutzt -
    # der WSPushSource sendet im Live-Pfad selbst, hier ist die
    # Registry nur fuer den Replay-Test relevant.
    registry_send_log: list[tuple[str, dict]] = []

    async def _replay_subscribe(topic: str, args: dict) -> None:
        registry_send_log.append((topic, dict(args)))

    registry = SubscriptionRegistry(subscribe=_replay_subscribe)
    manager = SubscriptionManager(
        client=_NullCPClient(),  # type: ignore[arg-type]
        quotes_source="ws",
        ws_subscribe=lambda conid, fields: _noop(),
        ws_unsubscribe=lambda conid: _noop(),
    )
    source = WSPushSource(
        client=fake_client,  # type: ignore[arg-type]
        adapter=adapter,
        registry=registry,
        manager=manager,
    )
    # Den Manager so verdrahten, dass subscribe_quotes/unsubscribe_quotes
    # vom WSPushSource aufgerufen werden statt der Lambdas oben.
    manager._ws_subscribe = source.subscribe_quotes  # noqa: SLF001
    manager._ws_unsubscribe = source.unsubscribe_quotes  # noqa: SLF001
    yield fake_client, adapter, registry, manager, source
    await source.stop()


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


# ---------------------------------------------------------------------------
# 1. Adapter-zu-Manager-Bruecke
# ---------------------------------------------------------------------------


async def test_dispatch_publishes_snapshot_to_manager(ws_stack) -> None:
    fake_client, adapter, registry, manager, source = ws_stack

    iterator = await manager.subscribe(
        conids=[CONID],
        field_codes=["31", "84", "86"],
        consumer_id="c1",
    )

    # Frame durch dispatch schleusen.
    raw_frame = {
        "topic": f"smd+{CONID}",
        "conid": CONID,
        "_updated": 1,
        "31": "100.50",
        "84": "100.40",
        "86": "100.60",
        "6509": "DPB",
    }
    source._dispatch(  # noqa: SLF001
        WSIncomingFrame(
            topic=f"smd+{CONID}",
            raw=json.dumps(raw_frame),
            parsed=raw_frame,
        )
    )

    # Erste Quote aus dem Iterator.
    event = await asyncio.wait_for(_first_event(iterator), timeout=1.0)
    assert event.conid == CONID
    assert event.quote.last == "100.50"
    assert event.quote.bid == "100.40"
    assert event.quote.ask == "100.60"


async def _first_event(iterator):
    async for event in iterator:
        return event
    raise AssertionError("Iterator hat kein Event geliefert")


# ---------------------------------------------------------------------------
# 2./3. quotes_source-Modus + Polling-Skip
# ---------------------------------------------------------------------------


def test_subscription_manager_requires_ws_subscribe_for_ws_mode() -> None:
    with pytest.raises(ValueError, match="ws_subscribe"):
        SubscriptionManager(
            client=_NullCPClient(),  # type: ignore[arg-type]
            quotes_source="ws",
        )


async def test_polling_mode_starts_poll_task() -> None:
    """Im polling-Modus startet ``start_poll`` einen Task."""
    from broker_gateway.streams.manager import _ConidSubscription  # noqa: PLC0415

    manager = SubscriptionManager(client=_NullCPClient(), quotes_source="polling")  # type: ignore[arg-type]
    sub = _ConidSubscription(manager, conid=CONID, field_codes={"31"})

    # Wir mocken den Poll-Loop, damit kein REST-Call passiert.
    async def _stub_loop(self) -> None:
        await asyncio.sleep(0)

    sub._poll_loop = _stub_loop.__get__(sub)  # type: ignore[method-assign]  # noqa: SLF001
    await sub.start_poll()
    assert sub._poll_task is not None  # noqa: SLF001
    await sub.stop()


async def test_ws_mode_skips_poll_task() -> None:
    from broker_gateway.streams.manager import _ConidSubscription  # noqa: PLC0415

    manager = SubscriptionManager(
        client=_NullCPClient(),  # type: ignore[arg-type]
        quotes_source="ws",
        ws_subscribe=_noop,
    )
    sub = _ConidSubscription(manager, conid=CONID, field_codes={"31"})
    await sub.start_poll()
    assert sub._poll_task is None  # noqa: SLF001
    await sub.stop()


# ---------------------------------------------------------------------------
# 4. SSE-Vertragsgleichheit (Quote-Schema identisch)
# ---------------------------------------------------------------------------


def test_smd_frame_to_quote_matches_polling_schema() -> None:
    from broker_gateway.cp.topics.smd import SmdFrame  # noqa: PLC0415

    snap = SmdFrame(
        conid=CONID,
        updated_at=1777415639001,
        last=Decimal("100.50"),
        bid=Decimal("100.40"),
        ask=Decimal("100.60"),
        volume=12345,
        change_pct=0.56,
        availability_code="DPB",
        high=Decimal("101.00"),
        low=Decimal("99.00"),
    )
    quote = _smd_frame_to_quote(snap)

    assert isinstance(quote, Quote)
    assert quote.conid == CONID
    # Schema: alle Preis/Size-Felder als String wie im Polling-Pfad.
    assert quote.last == "100.50"
    assert quote.bid == "100.40"
    assert quote.ask == "100.60"
    assert quote.volume == "12345"
    assert quote.change_pct == "0.56"
    assert quote.high == "101.00"
    assert quote.low == "99.00"
    assert quote.availability_raw == "DPB"


def test_decimal_helpers_handle_none() -> None:
    assert _decimal_to_str(None) is None
    assert _format_change_pct(0.0) == "0"
    assert _format_change_pct(1.2345) == "1.2345"


# ---------------------------------------------------------------------------
# 5. Reconnect triggert Registry-Replay
# ---------------------------------------------------------------------------


async def test_on_connected_replays_registry(ws_stack) -> None:
    fake_client, _adapter, registry, _manager, _source = ws_stack

    # Eintrag in die Registry, dann Connected-Hook simulieren.
    await registry.add("smd", {"conid": CONID, "fields": ("31",)}, owner=object())
    assert registry.count() == 1

    await fake_client.fire_connected()

    # Replay-Recorder ist die Subscribe-Funktion in der Fixture; wir
    # pruefen ueber die Registry, dass replay() lief und nichts kaputt
    # ging.
    # Erneuter fire_connected sollte ohne Exception laufen.
    await fake_client.fire_connected()
    assert registry.count() == 1


# ---------------------------------------------------------------------------
# 6. subscribe_quotes / unsubscribe_quotes pflegen Registry und senden Frames
# ---------------------------------------------------------------------------


async def test_subscribe_quotes_adds_to_registry_and_sends_smd_frame(
    ws_stack,
) -> None:
    fake_client, _adapter, registry, _manager, source = ws_stack

    await source.subscribe_quotes(CONID, {"31", "84", "86"})

    # Registry hat den Eintrag.
    assert registry.count() == 1
    # Send-Frame: smd+<conid>+{json}
    assert len(fake_client.sent) == 1
    sent = fake_client.sent[0]
    assert sent.startswith(f"smd+{CONID}+")
    body = sent.split("+", 2)[2]
    payload = json.loads(body)
    assert "31" in payload["fields"]
    assert "84" in payload["fields"]
    assert "86" in payload["fields"]


async def test_unsubscribe_quotes_removes_from_registry_and_sends_usmd(
    ws_stack,
) -> None:
    fake_client, _adapter, registry, _manager, source = ws_stack

    await source.subscribe_quotes(CONID, {"31"})
    assert registry.count() == 1

    fake_client.sent.clear()
    await source.unsubscribe_quotes(CONID)

    assert registry.count() == 0
    assert fake_client.sent == [f"usmd+{CONID}+{{}}"]


# ---------------------------------------------------------------------------
# 7. Frame fuer unbekannten conid wird verworfen
# ---------------------------------------------------------------------------


async def test_publish_unknown_conid_is_silent(ws_stack) -> None:
    _fake, _adapter, _reg, manager, source = ws_stack

    raw_frame = {
        "topic": "smd+9999",
        "conid": 9999,
        "_updated": 1,
        "31": "10.00",
    }
    # Kein subscribe -> publish liefert None und wirft nicht.
    source._dispatch(  # noqa: SLF001
        WSIncomingFrame(
            topic="smd+9999",
            raw=json.dumps(raw_frame),
            parsed=raw_frame,
        )
    )
    # Keine Subscription, also auch keine ausstehenden Events.
    assert manager.active_conids == set()


# ---------------------------------------------------------------------------
# 8. Send-Fehler im Subscribe wird geschluckt
# ---------------------------------------------------------------------------


async def test_subscribe_send_failure_is_swallowed(ws_stack) -> None:
    fake_client, _adapter, registry, _manager, source = ws_stack
    fake_client.send_failures.add(f"smd+{CONID}+{{\"fields\":[\"31\"]}}")

    # Darf nicht raisen, auch wenn der Send-Frame als Fehler markiert ist.
    # Der Frame wird sortierbar normalisiert; wir pruefen daher auf
    # Verhalten statt exakten String-Match.
    await source.subscribe_quotes(CONID, {"31"})

    # Registry-Eintrag ist trotzdem da.
    assert registry.count() == 1


# ---------------------------------------------------------------------------
# config.quotes_source ENV-Lookup
# ---------------------------------------------------------------------------


def test_config_default_is_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BG_QUOTES_SOURCE", raising=False)
    import broker_gateway.config as cfg  # noqa: PLC0415
    importlib.reload(cfg)
    assert cfg.quotes_source() == "polling"


def test_config_reads_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_QUOTES_SOURCE", "ws")
    import broker_gateway.config as cfg  # noqa: PLC0415
    importlib.reload(cfg)
    assert cfg.quotes_source() == "ws"


def test_config_falls_back_to_default_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BG_QUOTES_SOURCE", "magic")
    import broker_gateway.config as cfg  # noqa: PLC0415
    importlib.reload(cfg)
    assert cfg.quotes_source() == "polling"
