"""Generischer TTL-Cache.

Single Source of Truth für Caching im Service. Endpunkte und Adapter
dürfen keine eigenen ad-hoc-dicts halten - dieser Cache ist die
einzig zugelassene Form.
"""
from __future__ import annotations

import threading
import time
from typing import Generic, Hashable, TypeVar


K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    """In-Memory-Cache mit Time-to-Live pro Eintrag.

    Lazy-Expiry: abgelaufene Einträge werden erst beim nächsten `get`
    entfernt. Das hält den Cache simpel - kein zusätzlicher Reaper-Task.
    """

    def __init__(self, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds muss > 0 sein")
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._entries: dict[K, tuple[V, float]] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def get(self, key: K) -> tuple[V | None, bool]:
        """Liefert (value, hit). Bei Miss oder Expiry: (None, False)."""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None, False
            value, expires_at = entry
            if now >= expires_at:
                self._entries.pop(key, None)
                return None, False
            return value, True

    def set(self, key: K, value: V) -> None:
        expires_at = time.monotonic() + self._ttl
        with self._lock:
            self._entries[key] = (value, expires_at)

    def invalidate(self, key: K) -> bool:
        with self._lock:
            return self._entries.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
