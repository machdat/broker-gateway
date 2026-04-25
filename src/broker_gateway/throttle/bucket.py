"""Asynchroner Token-Bucket fuer Rate-Limit-Throttling.

Klassisches Modell: pro Sekunde fliessen `rate_per_s` Tokens nach, der
Bucket fasst maximal `capacity` Tokens. `acquire()` zieht ein Token; ist
keiner verfuegbar, wartet die Coroutine bis genug nachgefuellt wurde.

Erweiterung fuer Pacing-Violations (CP-Gateway HTTP 429): zusaetzlicher
`extra_wait_s`, der vor jedem `acquire()` als Vor-Wartezeit eingehalten
wird. `register_pacing_violation()` verdoppelt diesen Wert (mit Jitter)
bis zu `max_backoff_s`. Erfolgreiche Calls (`register_success()`)
reduzieren ihn schrittweise.

Single Source of Truth fuer die Bucket-Mechanik. ThrottleManager hat
kein eigenes Tokens-Tracking - er instanziiert nur Buckets.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time as _time_mod
from dataclasses import dataclass
from typing import Awaitable, Callable


logger = logging.getLogger(__name__)


_MIN_BACKOFF_S = 0.5
_DEFAULT_MAX_BACKOFF_S = 30.0
_DEFAULT_BACKOFF_JITTER = 0.2  # +/- 20 %


TimeProvider = Callable[[], float]
SleepFunction = Callable[[float], Awaitable[None]]


def _default_time() -> float:
    return _time_mod.monotonic()


async def _default_sleep(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


@dataclass
class BucketStats:
    acquired_total: int = 0
    wait_ms_total: float = 0.0
    pacing_violations_total: int = 0


class TokenBucket:
    def __init__(
        self,
        *,
        rate_per_s: float,
        capacity: float,
        max_backoff_s: float = _DEFAULT_MAX_BACKOFF_S,
        backoff_jitter: float = _DEFAULT_BACKOFF_JITTER,
        time_provider: TimeProvider | None = None,
        sleep_fn: SleepFunction | None = None,
        rng: random.Random | None = None,
    ) -> None:
        if rate_per_s <= 0:
            raise ValueError("rate_per_s muss > 0 sein")
        if capacity <= 0:
            raise ValueError("capacity muss > 0 sein")
        self.rate_per_s = float(rate_per_s)
        self.capacity = float(capacity)
        self.max_backoff_s = float(max_backoff_s)
        self.backoff_jitter = float(backoff_jitter)
        self._time = time_provider or _default_time
        self._sleep = sleep_fn or _default_sleep
        self._rng = rng or random.Random()

        self._tokens = float(capacity)
        self._last_refill_t = self._time()
        self._extra_wait_s = 0.0
        self._consecutive_successes = 0
        self._lock = asyncio.Lock()
        self.stats = BucketStats()

    @property
    def extra_wait_s(self) -> float:
        return self._extra_wait_s

    @property
    def tokens(self) -> float:
        return self._tokens

    async def acquire(self) -> float:
        """Wartet, bis ein Token verfuegbar ist; gibt die Wartezeit in
        Sekunden zurueck (0.0 wenn sofort verfuegbar).
        """
        wait_total = 0.0
        if self._extra_wait_s > 0.0:
            extra = self._extra_wait_s
            self._extra_wait_s = 0.0
            await self._sleep(extra)
            wait_total += extra

        while True:
            async with self._lock:
                now = self._time()
                elapsed = max(0.0, now - self._last_refill_t)
                self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_s)
                self._last_refill_t = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    self.stats.acquired_total += 1
                    self.stats.wait_ms_total += wait_total * 1000.0
                    return wait_total
                deficit = 1.0 - self._tokens
                wait_s = deficit / self.rate_per_s
            await self._sleep(wait_s)
            wait_total += wait_s

    def register_pacing_violation(self) -> None:
        """Verdoppelt das `extra_wait_s` (mit Jitter), gedeckelt auf
        max_backoff_s. Wird nach einer 429-Response aufgerufen.
        """
        base = max(_MIN_BACKOFF_S, self._extra_wait_s * 2.0 if self._extra_wait_s > 0.0 else _MIN_BACKOFF_S)
        jitter = base * self.backoff_jitter * (2.0 * self._rng.random() - 1.0)
        self._extra_wait_s = min(self.max_backoff_s, base + jitter)
        self._consecutive_successes = 0
        self.stats.pacing_violations_total += 1

    def register_success(self) -> None:
        """Recovery: nach 5 erfolgreichen Calls in Folge wird der Backoff
        zurueckgesetzt.
        """
        self._consecutive_successes += 1
        if self._consecutive_successes >= 5:
            self._extra_wait_s = 0.0


__all__ = ["BucketStats", "TokenBucket"]
