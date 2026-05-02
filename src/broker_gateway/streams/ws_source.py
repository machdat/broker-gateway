"""WSPushSource - Bruecke zwischen WS-Frames und SubscriptionManager.

Verbindet drei Komponenten zu einem laufenden Push-Pfad:

1. :class:`broker_gateway.cp.ws_client.CPWebSocketClient` als Frame-Quelle
   (Async-Iterator).
2. :class:`broker_gateway.cp.topics.smd.SmdTopicAdapter` als
   Frame-Transformer (rohes WS-Dict zu semantischem Voll-Snapshot).
3. :class:`broker_gateway.streams.registry.SubscriptionRegistry` als
   Soll-State-Speicher und Replay-Backbone.

Der ``WSPushSource`` selbst:

- haelt einen Hintergrund-Task, der vom WS-Client iteriert, jedes Frame
  durch den Adapter schickt und das resultierende Snapshot in den
  ``SubscriptionManager`` einspeist;
- registriert beim Konstruktor einen on-connected-Callback am WS-Client
  (``CPWebSocketClient.add_on_connected_callback``), der die Registry
  nach jedem Reconnect replayt;
- bietet zwei Async-Methoden, die der ``SubscriptionManager`` als
  ``ws_subscribe``/``ws_unsubscribe``-Callbacks nutzt: sie pflegen die
  Registry und schicken das passende Subscribe-/Unsubscribe-Frame an
  den WS-Client.

Frame-Format des Subscribe-Frames: ``smd+<conid>+<json>``, wobei
``<json>`` ein JSON-String mit den IBKR-Field-Codes ist
(z.B. ``{"fields":["31","84","86","6509"]}``). Der CPWebSocketClient
sendet diesen String als Plain-String-Frame; das CP-Gateway parst
intern.

Der Snapshot-zu-Quote-Mapper konvertiert ``SmdFrame`` (semantische
Decimals/ints) in :class:`broker_gateway.cp.quotes.Quote` (string-
basiertes Public-API-Modell). So bleibt der Egress-Vertrag des
``/v1/quotes/stream``-SSE-Endpunkts identisch zum Polling-Pfad.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from broker_gateway.availability import map_availability
from broker_gateway.cp.quotes import Quote
from broker_gateway.cp.topics.smd import SmdFrame, SmdTopicAdapter
from broker_gateway.cp.ws_client import CPWebSocketClient, WSIncomingFrame
from broker_gateway.streams.manager import SubscriptionManager
from broker_gateway.streams.registry import SubscriptionRegistry


logger = logging.getLogger(__name__)


# Default-Felder fuer den smd-Subscribe. Wenn ein Aufrufer (Manager)
# spezifische Felder mitgibt, werden sie zusaetzlich aufgenommen.
_DEFAULT_SMD_FIELDS: frozenset[str] = frozenset(
    {"31", "84", "86", "88", "85", "87", "83", "6509", "70", "71"}
)


class WSPushSource:
    """Verdrahtet WS-Frames zu ``SubscriptionManager.publish``-Calls.

    Der Konstruktor verdrahtet bereits den
    ``add_on_connected_callback``-Hook am Client - die Registry wird
    nach jedem Reconnect automatisch replayt.

    Der Reader-Task wird mit ``await source.start()`` gestartet und mit
    ``await source.stop()`` sauber beendet. Doppelte ``start()``-Aufrufe
    sind idempotent.
    """

    def __init__(
        self,
        client: CPWebSocketClient,
        adapter: SmdTopicAdapter,
        registry: SubscriptionRegistry,
        manager: SubscriptionManager,
    ) -> None:
        self._client = client
        self._adapter = adapter
        self._registry = registry
        self._manager = manager
        self._reader_task: asyncio.Task[None] | None = None
        self._stopped = False
        self._client.add_on_connected_callback(self._on_connected)

    # ---- Lifecycle ----

    async def start(self) -> None:
        """Startet den Reader-Task. Idempotent."""
        if self._reader_task is not None:
            return
        self._stopped = False
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="ws-push-source-reader"
        )

    async def stop(self) -> None:
        """Stoppt den Reader-Task. Idempotent."""
        self._stopped = True
        task = self._reader_task
        self._reader_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    # ---- Manager-Callables (subscribe / unsubscribe) ----

    async def subscribe_quotes(self, conid: int, field_codes: set[str]) -> None:
        """Manager-seitig genutzter Callback fuer einen neuen conid.

        Pflegt die Registry (Soll-State + Replay-Backbone), preloadet
        die Tradeability-Daten (exchange_id + Schedule, AP-11 K5) und
        schickt einen Subscribe-Frame an den WS-Client. Frame-Format:
        ``smd+<conid>+<json>`` mit ``fields``-Array aus dem
        ``field_codes``-Eingang plus den Defaults aus
        ``_DEFAULT_SMD_FIELDS``.
        """
        merged_fields = sorted(set(field_codes) | _DEFAULT_SMD_FIELDS)
        args = {"conid": conid, "fields": tuple(merged_fields)}
        await self._registry.add("smd", args, owner=self)
        # Tradeability-Felder brauchen den Schedule pro Boerse - der
        # Adapter haelt nach diesem Aufruf einen lokalen Cache und
        # kann ``feed()`` synchron mit Tradeability-Anreicherung
        # bedienen. Fehlende Lookups schlucken still (Adapter laeuft
        # ohne Tradeability-Felder weiter).
        try:
            await self._adapter.preload_for_conid(conid)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "WSPushSource: preload_for_conid(%s) fehlgeschlagen: %s",
                conid,
                exc,
            )
        await self._send_smd_subscribe(conid, merged_fields)

    async def unsubscribe_quotes(self, conid: int) -> None:
        """Manager-seitig genutzter Callback fuer ein Refcount-0-Teardown.

        Aus der Registry entfernen wir Eintraege, die exakt diesen conid
        haben (egal welche Felder); das Frame ``usmd+<conid>`` an den
        WS-Client beendet die Subscription serverseitig.
        """
        await self._registry_remove_all_for_conid(conid)
        await self._send_smd_unsubscribe(conid)

    # ---- internals ----

    async def _on_connected(self) -> None:
        """Hook, der nach Connect/Reconnect feuert. Replayt alle
        aktiven Subscriptions aus der Registry."""
        sent = await self._registry.replay()
        logger.info("WSPushSource: %s Subscriptions replayed", sent)

    async def _read_loop(self) -> None:
        try:
            async for frame in self._client:
                if self._stopped:
                    return
                self._dispatch(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("WSPushSource Reader-Loop beendet mit %s", exc)

    def _dispatch(self, frame: WSIncomingFrame) -> None:
        parsed = frame.parsed
        if not isinstance(parsed, dict):
            return
        snap = self._adapter.feed(parsed)
        if snap is None:
            return
        quote = _smd_frame_to_quote(snap)
        self._manager.publish(snap.conid, quote)

    async def _send_smd_subscribe(
        self, conid: int, field_codes: list[str]
    ) -> None:
        body = json.dumps({"fields": field_codes}, separators=(",", ":"))
        await _safe_ws_send(self._client, f"smd+{conid}+{body}")

    async def _send_smd_unsubscribe(self, conid: int) -> None:
        await _safe_ws_send(self._client, f"usmd+{conid}+{{}}")

    async def _registry_remove_all_for_conid(self, conid: int) -> None:
        # Die Registry haelt (topic, frozenset(args))-Schluessel; wir
        # entfernen alle Eintraege, deren ``conid`` matcht. In der
        # Praxis steckt pro conid nur ein Eintrag drin (gleiche
        # field_codes), aber der Defensiv-Pfad raeumt alles auf.
        async with self._registry._lock:  # noqa: SLF001
            keys_to_remove = [
                key
                for key, (topic, args, _owners) in self._registry._entries.items()  # noqa: SLF001
                if topic == "smd" and args.get("conid") == conid
            ]
            for key in keys_to_remove:
                del self._registry._entries[key]  # noqa: SLF001


async def _safe_ws_send(client: CPWebSocketClient, frame: str) -> None:
    """Sendet einen WS-Frame und schluckt Verbindungs-Fehler.

    Ein Send-Fehler hier ist kein Service-Stop: der Reconnect-Pfad
    schickt nach erfolgreichem Reconnect alle Subscribes via
    Registry-Replay neu - dabei greift derselbe Send-Pfad. Wenn der
    Send hier waehrend einer Reconnect-Pause fehlschlaegt, ist das
    erwartet und im Replay nachgeholt.
    """
    try:
        await client.send(frame)
    except RuntimeError as exc:
        # ``send vor connect oder nach close`` - im Reconnect-Fenster.
        logger.debug("WS-Send '%s' aufgeschoben: %s", frame, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("WS-Send '%s' fehlgeschlagen: %s", frame, exc)


def _smd_frame_to_quote(frame: SmdFrame) -> Quote:
    """Konvertiert einen ``SmdFrame`` (Decimal/int/...) in einen
    ``Quote`` (Public-API-Schema, String-Felder).

    Hintergrund: ``Quote`` ist mit allen Preis-/Size-Feldern als ``str``
    definiert (Anhang 1.5 des K6-Designs zu Snapshot-Roundtrip). Die
    interne Decimal-Genauigkeit aus dem Adapter wird hier in einen
    String serialisiert; das Ergebnis ist bitidentisch zum
    Polling-Pfad-Output.
    """
    return Quote(
        conid=frame.conid,
        last=_decimal_to_str(frame.last),
        bid=_decimal_to_str(frame.bid),
        ask=_decimal_to_str(frame.ask),
        volume=str(frame.volume) if frame.volume is not None else None,
        change_pct=(
            _format_change_pct(frame.change_pct)
            if frame.change_pct is not None
            else None
        ),
        high=_decimal_to_str(frame.high),
        low=_decimal_to_str(frame.low),
        availability=(
            map_availability(frame.availability_code)
            if frame.availability_code
            else None
        ),
        availability_raw=frame.availability_code,
        updated_at=_to_datetime(frame.updated_at),
    )


def _decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def _format_change_pct(value: float) -> str:
    return format(value, ".4f").rstrip("0").rstrip(".") or "0"


def _to_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # IBKR liefert Unix-Millisekunden.
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


__all__ = ["WSPushSource"]
