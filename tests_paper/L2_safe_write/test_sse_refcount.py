"""L2 paper_safe_write: SSE-Stream-Verhalten gegen Paper
(AP-12 L2-2).

Verifiziert das Subscription-Manager-Verhalten gegen die deployed
Paper-Instanz (polling-Pfad): zwei parallele Consumer auf demselben
conid sehen beide Frames, ein dritter Stream nach Schliessen der
ersten beiden funktioniert noch.

Hinweis Refcount-Status: Der ``/v1/status``-Endpoint zaehlt nur
SubscriptionRegistry-Eintraege (WS-Push-Pfad ab AP-11 K9). Im
polling-Default des Paper-Stacks ist
``subscriptions_active=0``, auch wenn ein Stream offen ist - der
Manager-Refcount lebt dann ausschliesslich in
``streams/manager.SubscriptionManager._subs``. Dieser Test
verifiziert daher das beobachtbare Verhalten direkt am Stream und
schaut nicht auf den Status-Endpoint-Counter.
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from tests_paper._dsl.actions import subscribe_quote_stream
from tests_paper._dsl.symbols import CONID_AAPL


pytestmark = pytest.mark.paper_safe_write


_DRAIN_TIMEOUT_S = 8.0
_COOL_DOWN_BUFFER_S = 7.0


async def _drain_one_event_block(stream: httpx.Response) -> str:
    """Liest mindestens einen vollstaendigen SSE-Block (data: ...) und
    liefert die gelesene data-Zeile zurueck.

    SSE-Frames terminieren mit Leerzeile. Wir warten auf die erste
    ``data:``-Zeile im Stream.
    """

    async def _read() -> str:
        async for line in stream.aiter_lines():
            if line.startswith("data:"):
                return line
        raise AssertionError("Stream beendet ohne data:-Frame")

    return await asyncio.wait_for(_read(), timeout=_DRAIN_TIMEOUT_S)


async def test_two_concurrent_streams_both_receive_frames(
    paper_http_client: httpx.AsyncClient,
) -> None:
    """Zwei parallele SSE-Consumer auf denselben conid bekommen jeweils
    mindestens einen ``data:``-Frame."""
    async with subscribe_quote_stream(
        paper_http_client, [CONID_AAPL], fields=["last"]
    ) as stream_a:
        async with subscribe_quote_stream(
            paper_http_client, [CONID_AAPL], fields=["last"]
        ) as stream_b:
            data_a, data_b = await asyncio.gather(
                _drain_one_event_block(stream_a),
                _drain_one_event_block(stream_b),
            )
        assert data_a.startswith("data:") and data_b.startswith("data:")
        assert "265598" in data_a and "265598" in data_b


async def test_stream_after_consumer_drop_still_works(
    paper_http_client: httpx.AsyncClient,
) -> None:
    """Nach Schliessen eines Streams + Cool-Down kann ein neuer Stream
    auf denselben conid erneut Frames empfangen."""
    async with subscribe_quote_stream(
        paper_http_client, [CONID_AAPL], fields=["last"]
    ) as first:
        await _drain_one_event_block(first)
    # Cool-Down auf Manager-Seite (5 s laut _DEFAULT_COOL_DOWN_S) plus
    # Sicherheits-Buffer fuer den IBKR-Unsubscribe-Roundtrip.
    await asyncio.sleep(_COOL_DOWN_BUFFER_S)
    async with subscribe_quote_stream(
        paper_http_client, [CONID_AAPL], fields=["last"]
    ) as second:
        data = await _drain_one_event_block(second)
    assert data.startswith("data:")
    assert "265598" in data
