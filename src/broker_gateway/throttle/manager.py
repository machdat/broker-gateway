"""ThrottleManager: Singleton-Container fuer Token-Buckets pro Endpoint-Klasse.

Klassen leiten sich aus dem CP-Gateway-Pfad ab. Die Klassifizierung lebt
ausschliesslich in `classify_path` - wenn ein neuer Pfad eingefuehrt
wird, wird er hier eingetragen, niemand sonst macht eine eigene Variante.

Default-Raten sind konservativ (IBKR ~50 msg/s pro Konto, gesamt). Sie
sind via ENV pro Klasse einstellbar:

    BG_THROTTLE_<CLASS>_RPS    - Tokens pro Sekunde
    BG_THROTTLE_<CLASS>_BURST  - Bucket-Kapazitaet (max gleichzeitig)

mit `<CLASS>` als Upper-Case-Variante des Klassennamens, z.B.
`BG_THROTTLE_QUOTES_SNAPSHOT_RPS=20`.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Literal

from broker_gateway.throttle.bucket import TokenBucket


logger = logging.getLogger(__name__)


ThrottleClass = Literal[
    "auth_lifecycle",
    "instruments",
    "quotes_snapshot",
    "quotes_stream",
    "portfolio",
    "orders",
    "trades",
    "events",
]

ALL_THROTTLE_CLASSES: tuple[ThrottleClass, ...] = (
    "auth_lifecycle",
    "instruments",
    "quotes_snapshot",
    "quotes_stream",
    "portfolio",
    "orders",
    "trades",
    "events",
)


# Default-Raten je Klasse: (rps, burst). Konservativ, IBKR-konform.
_DEFAULT_RATES: dict[ThrottleClass, tuple[float, float]] = {
    "auth_lifecycle":  (5.0, 10.0),  # Tickle alle 60 s + Reauth-Bursts
    "instruments":     (5.0, 10.0),
    "quotes_snapshot": (10.0, 20.0),
    "quotes_stream":   (10.0, 20.0),  # poll-Schleifen pro conid
    "portfolio":       (5.0, 10.0),
    "orders":          (5.0, 10.0),
    "trades":          (2.0, 5.0),
    "events":          (5.0, 10.0),
}


def classify_path(method: str, path: str) -> ThrottleClass:
    """Mappt (HTTP-Methode, CP-Gateway-Pfad) auf eine Throttle-Klasse.

    Single Source of Truth fuer die Klassifizierung. Unbekannte Pfade
    fallen sicherheitshalber auf `instruments` (niedrigste Auswirkung).
    """
    p = path.split("?", 1)[0].rstrip("/")

    if p == "/tickle" or p == "/reauthenticate" or p == "/iserver/auth/status":
        return "auth_lifecycle"
    if p.startswith("/iserver/secdef"):
        return "instruments"
    if p == "/iserver/marketdata/snapshot":
        return "quotes_snapshot"
    if re.match(r"^/iserver/marketdata/\d+/unsubscribe$", p):
        return "quotes_stream"
    if re.match(r"^/iserver/account/[^/]+/portfolio$", p):
        return "portfolio"
    if re.match(r"^/iserver/account/[^/]+/positions$", p):
        return "portfolio"
    if re.match(r"^/iserver/account/[^/]+/ledger$", p):
        return "portfolio"
    if re.match(r"^/iserver/account/[^/]+/orders$", p):
        return "orders"
    if re.match(r"^/iserver/account/orders/[^/]+$", p):
        return "orders"
    if re.match(r"^/iserver/account/[^/]+/order/[^/]+$", p):
        return "orders"
    if re.match(r"^/iserver/reply/[^/]+$", p):
        return "orders"
    if p == "/iserver/account/trades":
        return "trades"
    if p.startswith("/v1/api/ws"):
        return "events"

    logger.debug("classify_path: unbekannter Pfad %s, fallback auf instruments", p)
    return "instruments"


def _rate_from_env(cls: ThrottleClass, default: tuple[float, float]) -> tuple[float, float]:
    rps_env = os.environ.get(f"BG_THROTTLE_{cls.upper()}_RPS")
    burst_env = os.environ.get(f"BG_THROTTLE_{cls.upper()}_BURST")
    rps = default[0]
    burst = default[1]
    if rps_env:
        try:
            rps = float(rps_env)
        except ValueError:
            logger.warning("ENV %s=%r ist keine Zahl, ignoriert", f"BG_THROTTLE_{cls.upper()}_RPS", rps_env)
    if burst_env:
        try:
            burst = float(burst_env)
        except ValueError:
            logger.warning("ENV %s=%r ist keine Zahl, ignoriert", f"BG_THROTTLE_{cls.upper()}_BURST", burst_env)
    return rps, burst


class ThrottleManager:
    """Pro App-Lifespan eine Instanz. Haelt einen Bucket pro Klasse.

    `acquire(method, path)` ist die einzige API, die der CP-Client
    benoetigt. `register_pacing_violation(method, path)` wird nach
    HTTP 429 gerufen.
    """

    def __init__(
        self,
        *,
        bucket_overrides: dict[ThrottleClass, TokenBucket] | None = None,
    ) -> None:
        self._buckets: dict[ThrottleClass, TokenBucket] = {}
        for cls in ALL_THROTTLE_CLASSES:
            if bucket_overrides and cls in bucket_overrides:
                self._buckets[cls] = bucket_overrides[cls]
                continue
            rps, burst = _rate_from_env(cls, _DEFAULT_RATES[cls])
            self._buckets[cls] = TokenBucket(rate_per_s=rps, capacity=burst)

    def bucket(self, cls: ThrottleClass) -> TokenBucket:
        return self._buckets[cls]

    async def acquire(self, method: str, path: str) -> tuple[ThrottleClass, float]:
        cls = classify_path(method, path)
        wait = await self._buckets[cls].acquire()
        return cls, wait

    def register_pacing_violation(self, method: str, path: str) -> ThrottleClass:
        cls = classify_path(method, path)
        self._buckets[cls].register_pacing_violation()
        return cls

    def register_success(self, method: str, path: str) -> ThrottleClass:
        cls = classify_path(method, path)
        self._buckets[cls].register_success()
        return cls

    def metrics(self) -> dict[ThrottleClass, dict[str, float]]:
        out: dict[ThrottleClass, dict[str, float]] = {}
        for cls, bucket in self._buckets.items():
            out[cls] = {
                "acquired_total": float(bucket.stats.acquired_total),
                "wait_ms_total": float(bucket.stats.wait_ms_total),
                "pacing_violations_total": float(bucket.stats.pacing_violations_total),
                "extra_wait_s": float(bucket.extra_wait_s),
            }
        return out


def get_throttle_manager() -> ThrottleManager:
    raise RuntimeError(
        "get_throttle_manager muss in der App per dependency_overrides gesetzt werden"
    )


__all__ = [
    "ALL_THROTTLE_CLASSES",
    "ThrottleClass",
    "ThrottleManager",
    "classify_path",
    "get_throttle_manager",
]
