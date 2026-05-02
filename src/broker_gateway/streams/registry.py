"""SubscriptionRegistry - Soll-State der aktiven WS-Subscriptions.

Das CP-Gateway persistiert den Subscription-State **nicht**: nach jedem
Reconnect sind alle ``smd``/``sor``-Abos serverseitig vergessen
(K4-Reconnect-Befund). Damit der WS-Push-Pfad nach einem Reconnect nicht
stillsteht, fuehrt diese Klasse den expliziten Soll-State im Service:
welche Topics mit welchen Args sind aktuell von Consumern referenziert?

Verantwortlichkeiten

- ``add(topic, args, owner)`` registriert eine Subscription, erhoeht den
  Refcount fuer den Lookup-Schluessel ``(topic, frozenset(args.items()))``.
- ``remove(topic, args, owner)`` reduziert den Refcount; bei 0 wird der
  Eintrag gestrichen.
- ``replay()`` schickt einen Subscribe-Call fuer jede aktive
  Subscription gegen den injizierten Subscribe-Callable. Genau das
  macht der ``CPWebSocketClient.add_on_connected_callback``-Pfad nach
  Reconnect.
- ``pause()`` / ``resume()`` friert Replays ein, solange das
  ``AuthLifecycle`` keinen ``AUTHENTICATED``-Zustand meldet. ``add`` und
  ``remove`` bleiben funktional - sie pflegen den Soll-State weiter, der
  Replay holt das nach.
- ``count()`` liefert die Anzahl aktiver Eintraege (PSM-/Status-Endpoint
  fuer AP-11 K8).

Was die Registry **nicht** tut

- Sie reauthenticiert nicht. ``AuthLifecycle.reauthenticate(force=True)``
  bleibt das einzige Reauth-Tor.
- Sie persistiert nichts auf Disk - bei Service-Restart faellt der State
  weg und wird durch die naechsten Consumer-Streams neu aufgebaut
  (Aufgabe von K3 ``WSPushSource``).
- Sie kennt den ``SubscriptionManager`` aus ``streams/manager.py``
  nicht; die Bruecke in den Refcount-Layer baut K3.

Thread-Safety

- Operationen werden aus dem asyncio-Reader-Loop und aus
  Endpoint-Handlern aufgerufen. ``asyncio.Lock`` haelt den Soll-State
  konsistent; keine ``threading``-Locks (kein Multi-Thread-Modell im
  Service).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any


logger = logging.getLogger(__name__)


SubscribeCallable = Callable[[str, dict[str, Any]], Awaitable[None]]


_SubscriptionKey = tuple[str, frozenset[tuple[str, Any]]]


class SubscriptionRegistry:
    """Hash-basierter Soll-State-Speicher fuer aktive WS-Subscriptions.

    Konstruiert mit einem ``subscribe``-Callable, das pro
    ``(topic, args)``-Paar den IBKR-spezifischen Subscribe-Frame sendet
    (z.B. ``await client.send(f"smd+{conid}+...")``). Damit ist die
    Registry vom konkreten ``CPWebSocketClient``-Frame-Format entkoppelt
    und gegen einen Fake testbar.
    """

    def __init__(self, subscribe: SubscribeCallable) -> None:
        self._subscribe = subscribe
        self._lock = asyncio.Lock()
        # Pro Lookup-Schluessel: das Set aller Owner (id-basiert) plus die
        # Original-Argumente (das ``frozenset`` im Schluessel ist hashbar,
        # aber die Reihenfolge / Originalstruktur brauchen wir fuer den
        # Subscribe-Call). Refcount = ``len(owners)``.
        self._entries: dict[
            _SubscriptionKey,
            tuple[str, dict[str, Any], set[int]],
        ] = {}
        self._paused = False

    # ---- public API ----

    async def add(
        self, topic: str, args: dict[str, Any], owner: object
    ) -> int:
        """Registriert eine Subscription. Liefert den neuen Refcount.

        Doppelte ``add(topic, args, owner)``-Aufrufe mit demselben
        ``owner`` zaehlen nur einfach (Idempotenz pro Owner).
        """
        key = _make_key(topic, args)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self._entries[key] = (topic, dict(args), {id(owner)})
                return 1
            existing_topic, existing_args, owners = entry
            owners.add(id(owner))
            return len(owners)

    async def remove(
        self, topic: str, args: dict[str, Any], owner: object
    ) -> int:
        """Reduziert den Refcount. Liefert den verbleibenden Refcount;
        0, wenn der Eintrag entfernt wurde.

        Aufrufe fuer unbekannte Schluessel oder unbekannte Owner sind
        idempotent (kein Fehler).
        """
        key = _make_key(topic, args)
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return 0
            existing_topic, existing_args, owners = entry
            owners.discard(id(owner))
            if not owners:
                del self._entries[key]
                return 0
            return len(owners)

    async def replay(self) -> int:
        """Schickt einen Subscribe pro aktivem Eintrag.

        Liefert die Anzahl tatsaechlich gesendeter Subscribes. Wenn
        ``pause()`` aktiv ist, wird kein Subscribe gesendet und ``0``
        zurueckgeliefert - der Caller weiss damit, dass der Replay
        ausstehend ist und nach ``resume()`` neu geschickt werden muss.

        Subscribe-Fehler werden geloggt, aber nicht propagiert: ein
        Reconnect-Hook darf nicht durch einen einzelnen fehlerhaften
        Subscribe abreissen.
        """
        if self._paused:
            logger.debug(
                "SubscriptionRegistry.replay uebersprungen, paused=True"
            )
            return 0
        async with self._lock:
            entries = list(self._entries.values())
        sent = 0
        for topic, args, _owners in entries:
            try:
                await self._subscribe(topic, args)
                sent += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SubscriptionRegistry.replay: subscribe(%r, %r) "
                    "fehlgeschlagen: %s",
                    topic,
                    args,
                    exc,
                )
        return sent

    def pause(self) -> None:
        """Friert ``replay()`` ein.

        ``add`` und ``remove`` bleiben funktional - der Soll-State wird
        weiter gepflegt, nur der Replay an den Wire-Pfad ist gesperrt
        (z.B. waehrend ``AuthLifecycle`` nicht ``AUTHENTICATED``).
        """
        self._paused = True

    def resume(self) -> None:
        """Hebt das Replay-Embargo aus ``pause()`` wieder auf."""
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def count(self) -> int:
        """Anzahl aktiver Subscription-Schluessel (refcount > 0)."""
        return len(self._entries)


def _make_key(topic: str, args: dict[str, Any]) -> _SubscriptionKey:
    """Baut einen hashbaren Schluessel aus ``topic`` und ``args``.

    ``args`` muss ausschliesslich hashbare Werte enthalten - in der
    Praxis sind das ``conid: int``, ``account: str``, ``fields:
    tuple[str, ...]``. Wenn ein Aufrufer eine Liste reinreicht, wird
    sie defensiv zu einem Tuple konvertiert, damit der Schluessel hashbar
    bleibt.
    """
    items: list[tuple[str, Any]] = []
    for key, value in args.items():
        if isinstance(value, list):
            value = tuple(value)
        items.append((key, value))
    return topic, frozenset(items)
