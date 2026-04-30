"""IBKR Client Portal Gateway - WebSocket-Client als wiederverwendbarer Baustein.

Kapselt die Lifecycle-Aufgaben einer WS-Verbindung gegen das interne
CP-Gateway:

- Connect (``ws://`` per Default, ``wss://`` per ENV-Override
  ``BG_CP_WS_URL``).
- Auth-Frame ``{"session": "<id>"}`` senden und auf das initiale
  ``sts``-Frame mit ``authenticated=true`` warten.
- Async-Iterator ueber eingehende Frames (JSON oder Plain-String wie
  ``tic``).
- Send-Methode fuer Outbound-Frames als Plain-String (Aufrufer baut
  Subscribe-Frames im IBKR-Format ``TOPIC+{...}`` selbst zusammen - dieser
  Client haelt keine Topic-Schema-Logik).
- ``tic``-Ping-Loop alle 30 s im Hintergrund.
- Reconnect mit exponential backoff + neuem Auth-Flow.
- Single-Owner: pro Instanz nur ein ``connect()``-Aufruf erlaubt.

NICHT in K3: Topic-Subscriptions, Konsum-Integration, EventBus-Mapping,
``tic``-Dedup. Das ist Foundation - Konsumenten kommen spaeter
(siehe AP-04 K6 + Folge-AP).

Die Klasse ist gegen einen injizierbaren ``connect_factory`` testbar - die
Tests verwenden einen In-Memory-FakeConnection statt eines echten Sockets.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


logger = logging.getLogger(__name__)


_DEFAULT_URL = "ws://cpgateway:5000/v1/api/ws"
_URL_ENV = "BG_CP_WS_URL"

_DEFAULT_PING_INTERVAL_S = 30.0
_DEFAULT_AUTH_TIMEOUT_S = 10.0
_DEFAULT_MAX_RECONNECT_ATTEMPTS = 3
_DEFAULT_RECONNECT_BACKOFF_S = 2.0
_DEFAULT_BACKOFF_FACTOR = 2.0


@dataclass(frozen=True)
class WSIncomingFrame:
    """Ein eingehender Frame in der Form, in der Konsumenten ihn nutzen.

    ``raw`` ist der Wire-String, ``parsed`` das dekodierte JSON falls der
    Frame parsbar war. ``topic`` wird aus ``parsed["topic"]`` gelesen, sonst
    ``"?"``. Schema bewusst aehnlich zu ``tests.cp_mock.ws_replay.WSFrame``,
    aber ohne ``ts``/``dir`` - der Live-Frame kommt nicht aus einem
    Recording.
    """

    topic: str
    raw: str
    parsed: Any | None = None


class WSAuthError(RuntimeError):
    """Server hat ``sts.authenticated=false`` zurueckgegeben oder kein
    Auth-Ack innerhalb des Timeouts geschickt."""


class WSConnection(Protocol):
    """Minimales Subset der Websocket-Connection-API, das wir nutzen.

    Implementiert sowohl von ``websockets.client.ClientConnection`` als auch
    vom Test-FakeConnection. Wir vermeiden bewusst das ``async with``-Muster
    aus ``websockets``, weil wir die Verbindung im Reconnect-Loop aktiv
    ersetzen wollen.
    """

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str: ...

    async def close(self) -> None: ...


ConnectFactory = Callable[[str, "str | None"], Awaitable[WSConnection]]


async def _default_connect(url: str, cookies: str | None) -> WSConnection:
    """Default-Factory: oeffnet eine echte ``websockets``-Connection.

    Cookie-Reuse aus dem REST-Client kommt explizit per Methodenparameter
    rein (kein direkter Import-Coupling). CA-Bundle-Strategie liegt
    komplett bei der ``websockets``-Lib - die Karte verbietet, TLS-
    Verifikation abzuschalten.
    """
    import websockets

    extra_headers: list[tuple[str, str]] = []
    if cookies:
        extra_headers.append(("Cookie", cookies))
    return await websockets.connect(  # type: ignore[return-value]
        url, additional_headers=extra_headers
    )


class CPWebSocketClient:
    """WS-Client gegen das interne CP Gateway.

    Lifecycle:

    - ``await client.connect(session_id, cookies)`` - oeffnet die
      Verbindung, sendet den Auth-Frame, wartet auf das initiale
      ``sts``-Frame mit ``authenticated=true``. Nach Erfolg laeuft im
      Hintergrund der ``tic``-Ping-Loop und ein Reader, der eingehende
      Frames in eine interne Queue legt.
    - ``async for frame in client`` - eingehende Frames als
      :class:`WSIncomingFrame`. Endet (``StopAsyncIteration``), sobald die
      Verbindung endgueltig (alle Reconnect-Versuche aufgebraucht oder
      ``aclose()`` gerufen) verloren ist.
    - ``await client.send(frame)`` - sendet einen Outbound-Frame als
      Plain-String.
    - ``await client.aclose()`` - stoppt Hintergrund-Tasks, schliesst die
      Verbindung. Idempotent.

    Single-Owner-Konstraint: pro Instanz darf ``connect()`` nur einmal
    aufgerufen werden. Ein zweiter Aufruf wirft ``RuntimeError``.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        ping_interval_s: float = _DEFAULT_PING_INTERVAL_S,
        max_reconnect_attempts: int = _DEFAULT_MAX_RECONNECT_ATTEMPTS,
        reconnect_backoff_s: float = _DEFAULT_RECONNECT_BACKOFF_S,
        backoff_factor: float = _DEFAULT_BACKOFF_FACTOR,
        connect_factory: ConnectFactory | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.url = url or os.environ.get(_URL_ENV) or _DEFAULT_URL
        self.ping_interval_s = ping_interval_s
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_backoff_s = reconnect_backoff_s
        self.backoff_factor = backoff_factor

        self._connect: ConnectFactory = connect_factory or _default_connect
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep

        self._connect_called = False
        self._closing = False
        self._connection: WSConnection | None = None

        self._session_id: str | None = None
        self._cookies: str | None = None

        self._frame_queue: asyncio.Queue[WSIncomingFrame | _Sentinel] = (
            asyncio.Queue()
        )
        self._reader_task: asyncio.Task[None] | None = None
        self._pinger_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return self._connection is not None and not self._closing

    # ---- public API ----

    async def connect(
        self,
        session_id: str,
        cookies: str | None = None,
        *,
        auth_timeout_s: float = _DEFAULT_AUTH_TIMEOUT_S,
    ) -> None:
        if self._connect_called:
            raise RuntimeError(
                "CPWebSocketClient.connect() pro Instanz nur einmal erlaubt - "
                "Single-Owner-Konstraint"
            )
        self._connect_called = True
        self._session_id = session_id
        self._cookies = cookies

        await self._open_and_authenticate(auth_timeout_s=auth_timeout_s)
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="cp-ws-reader"
        )
        self._pinger_task = asyncio.create_task(
            self._ping_loop(), name="cp-ws-pinger"
        )

    async def send(self, frame: str) -> None:
        if not self.connected or self._connection is None:
            raise RuntimeError(
                "CPWebSocketClient.send vor connect oder nach close"
            )
        await self._connection.send(frame)

    def __aiter__(self) -> AsyncIterator[WSIncomingFrame]:
        return self

    async def __anext__(self) -> WSIncomingFrame:
        item = await self._frame_queue.get()
        if isinstance(item, _Sentinel):
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        already_closing = self._closing
        self._closing = True
        # Pinger zuerst stoppen, sonst kann er waehrend des Close noch senden.
        for task in (self._pinger_task, self._reader_task):
            if task is None or task.done():
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._reader_task = None
        self._pinger_task = None
        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Fehler beim Schliessen der WS-Verbindung", exc_info=True
                )
            self._connection = None
        # Iteratoren entblocken, falls sie auf get() warten. Bei einem
        # zweiten aclose-Aufruf nicht erneut, sonst staut sich die Queue
        # mit Sentinels.
        if not already_closing:
            await self._frame_queue.put(_SENTINEL)

    # ---- internals ----

    async def _open_and_authenticate(self, *, auth_timeout_s: float) -> None:
        connection = await self._connect(self.url, self._cookies)
        try:
            auth_frame = json.dumps({"session": self._session_id})
            await connection.send(auth_frame)
            await self._wait_for_auth_ack(connection, timeout_s=auth_timeout_s)
        except BaseException:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001
                logger.debug(
                    "Fehler beim Schliessen waehrend Auth", exc_info=True
                )
            raise
        self._connection = connection

    async def _wait_for_auth_ack(
        self, connection: WSConnection, *, timeout_s: float
    ) -> None:
        async def _consume() -> None:
            while True:
                raw = await connection.recv()
                parsed = _safe_json(raw)
                if not isinstance(parsed, dict):
                    continue
                if parsed.get("topic") != "sts":
                    continue
                args = parsed.get("args")
                authenticated = isinstance(args, dict) and bool(
                    args.get("authenticated")
                )
                if authenticated:
                    return
                raise WSAuthError(
                    f"sts-Frame meldet authenticated=false: {parsed!r}"
                )

        try:
            await asyncio.wait_for(_consume(), timeout=timeout_s)
        except asyncio.TimeoutError as exc:
            raise WSAuthError(
                f"Kein sts-Auth-Ack innerhalb {timeout_s}s"
            ) from exc

    async def _read_loop(self) -> None:
        try:
            while not self._closing:
                connection = self._connection
                if connection is None:
                    break
                try:
                    raw = await connection.recv()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if self._closing:
                        break
                    logger.warning(
                        "WS recv-Fehler, starte Reconnect: %s", exc
                    )
                    if not await self._reconnect():
                        # Alle Reconnect-Versuche aufgebraucht: Klient ist
                        # effektiv tot - Pinger und kuenftige aclose-Aufrufe
                        # ueber das Closing-Flag stoppen.
                        self._closing = True
                        break
                    continue
                await self._frame_queue.put(_build_frame(raw))
        except asyncio.CancelledError:
            raise
        finally:
            await self._frame_queue.put(_SENTINEL)

    async def _ping_loop(self) -> None:
        try:
            while not self._closing:
                await self._sleep(self.ping_interval_s)
                if self._closing:
                    break
                connection = self._connection
                if connection is None:
                    continue
                try:
                    await connection.send("tic")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.debug("WS tic-Send fehlgeschlagen: %s", exc)
        except asyncio.CancelledError:
            raise

    async def _reconnect(self) -> bool:
        old = self._connection
        self._connection = None
        if old is not None:
            try:
                await old.close()
            except Exception:  # noqa: BLE001
                pass
        delay = self.reconnect_backoff_s
        for attempt in range(1, self.max_reconnect_attempts + 1):
            if self._closing:
                return False
            try:
                await self._open_and_authenticate(
                    auth_timeout_s=_DEFAULT_AUTH_TIMEOUT_S
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WS-Reconnect-Versuch %s/%s fehlgeschlagen: %s",
                    attempt,
                    self.max_reconnect_attempts,
                    exc,
                )
                if attempt >= self.max_reconnect_attempts:
                    return False
                await self._sleep(delay)
                delay *= self.backoff_factor
                continue
            logger.info("WS-Reconnect erfolgreich nach Versuch %s", attempt)
            return True
        return False


class _Sentinel:
    """Marker-Wert in :attr:`CPWebSocketClient._frame_queue`, der den
    Iterator beendet."""


_SENTINEL = _Sentinel()


def _safe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _build_frame(raw: str) -> WSIncomingFrame:
    parsed = _safe_json(raw)
    if isinstance(parsed, dict):
        topic_value = parsed.get("topic")
        topic = topic_value if isinstance(topic_value, str) else "?"
        return WSIncomingFrame(topic=topic, raw=raw, parsed=parsed)
    return WSIncomingFrame(topic="?", raw=raw, parsed=None)
