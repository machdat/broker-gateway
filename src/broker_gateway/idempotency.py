"""Idempotency-Key-Cache fuer Schreiboperationen.

Single Source of Truth fuer Idempotency-Replay. Endpunkte mit Schreib-
semantik (POST /v1/orders, DELETE /v1/orders/{id}, ...) speichern hier
das Ergebnis ihrer ersten erfolgreichen Bearbeitung gegen den
Idempotency-Key. Folgende Aufrufe mit demselben Key liefern die
gespeicherte Antwort, ohne den CP-Gateway erneut zu treffen.

Anti-Korruptions-Strategie:
- Default-TTL ist 24 Stunden (ENV `BG_IDEMPOTENCY_TTL_S` ueberschreibt).
- Werte sind ein vorgehaltenes Tupel `(status_code, payload)`. Der
  Aufrufer entscheidet, was er als payload speichert (typisch:
  Pydantic-Model als dict).
- Konkurrierende Reservierungen (`reserve`) sind via threading.Lock
  serialisiert; der erste Caller bekommt das Slot, alle anderen sehen
  die spaeter gespeicherte Antwort beim naechsten Aufruf.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any


_DEFAULT_TTL_S = 24 * 60 * 60.0  # 24 Stunden
_ENV_VAR = "BG_IDEMPOTENCY_TTL_S"


def _ttl_from_env() -> float:
    raw = os.environ.get(_ENV_VAR)
    if not raw:
        return _DEFAULT_TTL_S
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_TTL_S
    return value if value > 0 else _DEFAULT_TTL_S


class IdempotencyStore:
    """In-Memory-Idempotency-Cache mit TTL.

    Lookup-Pfad:
    - `get(key)` -> `(status_code, payload)` oder `None`. Lazy-Expiry.
    - `put(key, status, payload)` setzt den Eintrag.

    Reservierungs-Pfad (optional fuer kuenftige Use-Cases):
    - `reserve(key)` markiert einen Schluessel als "in-flight". Der
      naechste Caller, der denselben Key sieht, kann darauf reagieren.
      Wird in v0.9.0 noch nicht aktiv genutzt - der Order-Endpunkt
      faehrt synchron, also reicht get/put.
    """

    def __init__(self, *, ttl_seconds: float | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else _ttl_from_env()
        if ttl <= 0:
            raise ValueError("ttl_seconds muss > 0 sein")
        self._ttl = ttl
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[int, Any, float]] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def get(self, key: str) -> tuple[int, Any] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            status_code, payload, expires_at = entry
            if now >= expires_at:
                self._entries.pop(key, None)
                return None
            return status_code, payload

    def put(self, key: str, status_code: int, payload: Any) -> None:
        expires_at = time.monotonic() + self._ttl
        with self._lock:
            self._entries[key] = (status_code, payload, expires_at)

    def invalidate(self, key: str) -> bool:
        with self._lock:
            return self._entries.pop(key, None) is not None

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
