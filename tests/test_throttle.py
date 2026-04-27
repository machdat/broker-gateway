"""Tests fuer TokenBucket + ThrottleManager + CP-Client-Hook."""
from __future__ import annotations

import asyncio
import random

import httpx
import pytest
import respx

from broker_gateway.cp.client import CPGatewayClient
from broker_gateway.throttle.bucket import TokenBucket
from broker_gateway.throttle.manager import (
    ALL_THROTTLE_CLASSES,
    ThrottleManager,
    classify_path,
)


# ---- Deterministischer Time-/Sleep-Provider fuer den Bucket ----


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleep_log: list[float] = []

    def time(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        # Modelliert vergangene Zeit ohne echtes Warten.
        self.sleep_log.append(seconds)
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def bucket(clock: FakeClock) -> TokenBucket:
    return TokenBucket(
        rate_per_s=10.0,
        capacity=5.0,
        time_provider=clock.time,
        sleep_fn=clock.sleep,
        rng=random.Random(42),
    )


# ---- Bucket: Konsumtion + Wait ----


async def test_bucket_drains_initial_capacity_without_waiting(bucket: TokenBucket, clock: FakeClock) -> None:
    for _ in range(5):
        wait = await bucket.acquire()
        assert wait == 0.0
    assert clock.sleep_log == []


async def test_bucket_blocks_after_capacity_until_refill(bucket: TokenBucket, clock: FakeClock) -> None:
    for _ in range(5):
        await bucket.acquire()
    # 6. acquire muss warten - bei 10/s: 1 Token in 0.1 s.
    wait = await bucket.acquire()
    assert wait == pytest.approx(0.1, rel=0.05)


async def test_bucket_refills_over_time(bucket: TokenBucket, clock: FakeClock) -> None:
    for _ in range(5):
        await bucket.acquire()
    # Simuliere 0.5 s Pause (5 Tokens nach).
    clock.t += 0.5
    # Naechste 5 Acquires sollten ohne Warten durchgehen.
    for _ in range(5):
        wait = await bucket.acquire()
        assert wait == 0.0


async def test_bucket_pacing_violation_adds_extra_wait(bucket: TokenBucket, clock: FakeClock) -> None:
    bucket.register_pacing_violation()
    extra_first = bucket.extra_wait_s
    assert extra_first > 0.0

    # Naechster acquire muss extra warten.
    wait = await bucket.acquire()
    assert wait >= extra_first * 0.7  # mit Jitter +/- 20 %


async def test_bucket_pacing_violation_doubles_each_call(bucket: TokenBucket, clock: FakeClock) -> None:
    bucket.register_pacing_violation()
    first = bucket.extra_wait_s
    bucket.register_pacing_violation()
    second = bucket.extra_wait_s
    assert second > first


async def test_bucket_pacing_violation_capped_by_max_backoff(clock: FakeClock) -> None:
    b = TokenBucket(
        rate_per_s=10.0,
        capacity=5.0,
        max_backoff_s=2.0,
        time_provider=clock.time,
        sleep_fn=clock.sleep,
        rng=random.Random(1),
    )
    for _ in range(20):
        b.register_pacing_violation()
    assert b.extra_wait_s <= 2.0 + 0.5  # plus moegliche Jitter-Toleranz, gedeckelt


async def test_bucket_recovery_after_5_successes(bucket: TokenBucket) -> None:
    bucket.register_pacing_violation()
    assert bucket.extra_wait_s > 0.0
    for _ in range(5):
        bucket.register_success()
    assert bucket.extra_wait_s == 0.0


async def test_bucket_invalid_rate_raises() -> None:
    with pytest.raises(ValueError):
        TokenBucket(rate_per_s=0.0, capacity=5.0)
    with pytest.raises(ValueError):
        TokenBucket(rate_per_s=10.0, capacity=0.0)


# ---- ThrottleManager: Klassifizierung ----


def test_classify_auth_lifecycle_paths() -> None:
    assert classify_path("POST", "/tickle") == "auth_lifecycle"
    assert classify_path("POST", "/reauthenticate") == "auth_lifecycle"
    assert classify_path("GET", "/iserver/auth/status") == "auth_lifecycle"


def test_classify_quotes_paths() -> None:
    assert classify_path("GET", "/iserver/marketdata/snapshot?conids=1") == "quotes_snapshot"
    assert classify_path("GET", "/iserver/marketdata/265598/unsubscribe") == "quotes_stream"


def test_classify_orders_paths() -> None:
    assert classify_path("POST", "/iserver/account/U25/orders") == "orders"
    assert classify_path("GET", "/iserver/account/orders/12345") == "orders"
    assert classify_path("DELETE", "/iserver/account/U25/order/12345") == "orders"
    assert classify_path("POST", "/iserver/reply/abc-123") == "orders"


def test_classify_portfolio_paths() -> None:
    assert classify_path("GET", "/portfolio/U25/summary") == "portfolio"
    assert classify_path("GET", "/portfolio/U25/positions/0") == "portfolio"
    assert classify_path("GET", "/portfolio/U25/positions/3") == "portfolio"
    assert classify_path("GET", "/portfolio/U25/ledger") == "portfolio"


def test_classify_trades_path() -> None:
    assert classify_path("GET", "/iserver/account/trades?days=7") == "trades"


def test_classify_instruments_paths() -> None:
    assert classify_path("GET", "/iserver/secdef/search?symbol=AAPL") == "instruments"
    assert classify_path("GET", "/iserver/secdef/info?conid=1") == "instruments"


def test_classify_unknown_falls_back_to_instruments() -> None:
    assert classify_path("GET", "/some/unknown/path") == "instruments"


# ---- ThrottleManager: Buckets pro Klasse ----


def test_manager_has_one_bucket_per_class() -> None:
    mgr = ThrottleManager()
    for cls in ALL_THROTTLE_CLASSES:
        assert mgr.bucket(cls) is not None


async def test_manager_pacing_violation_only_on_classified_bucket() -> None:
    mgr = ThrottleManager()
    mgr.register_pacing_violation("GET", "/iserver/marketdata/snapshot")
    assert mgr.bucket("quotes_snapshot").extra_wait_s > 0.0
    # auth_lifecycle bleibt unbeeinflusst - das ist der Kern der Karte.
    assert mgr.bucket("auth_lifecycle").extra_wait_s == 0.0


def test_manager_metrics_initial_zero() -> None:
    mgr = ThrottleManager()
    metrics = mgr.metrics()
    for cls in ALL_THROTTLE_CLASSES:
        assert metrics[cls]["acquired_total"] == 0.0
        assert metrics[cls]["pacing_violations_total"] == 0.0


async def test_manager_acquire_records_metrics() -> None:
    mgr = ThrottleManager()
    cls, _ = await mgr.acquire("GET", "/iserver/marketdata/snapshot")
    assert cls == "quotes_snapshot"
    metrics = mgr.metrics()
    assert metrics["quotes_snapshot"]["acquired_total"] == 1.0


# ---- ENV-Konfiguration ----


def test_env_overrides_default_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_THROTTLE_QUOTES_SNAPSHOT_RPS", "100")
    monkeypatch.setenv("BG_THROTTLE_QUOTES_SNAPSHOT_BURST", "50")
    mgr = ThrottleManager()
    bucket = mgr.bucket("quotes_snapshot")
    assert bucket.rate_per_s == 100.0
    assert bucket.capacity == 50.0


def test_env_invalid_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BG_THROTTLE_QUOTES_SNAPSHOT_RPS", "not-a-number")
    mgr = ThrottleManager()
    bucket = mgr.bucket("quotes_snapshot")
    # Default ist 10.0 rps fuer quotes_snapshot
    assert bucket.rate_per_s == 10.0


# ---- Tickle-Bucket isoliert (Karten-Constraint) ----


async def test_tickle_bucket_unblocked_by_quotes_pressure(clock: FakeClock) -> None:
    """Karten-Constraint: Tickle-Job laeuft auch unter starkem Quotes-Last weiter."""
    overrides = {
        "auth_lifecycle": TokenBucket(
            rate_per_s=5.0, capacity=10.0,
            time_provider=clock.time, sleep_fn=clock.sleep, rng=random.Random(1),
        ),
        "quotes_snapshot": TokenBucket(
            rate_per_s=2.0, capacity=2.0,
            time_provider=clock.time, sleep_fn=clock.sleep, rng=random.Random(2),
        ),
    }
    mgr = ThrottleManager(bucket_overrides=overrides)

    # Quotes-Bucket ausschoepfen + Pacing-Violation.
    await mgr.acquire("GET", "/iserver/marketdata/snapshot")
    await mgr.acquire("GET", "/iserver/marketdata/snapshot")
    mgr.register_pacing_violation("GET", "/iserver/marketdata/snapshot")

    # Tickle-Acquire darf trotzdem ohne Warten durchgehen.
    cls, wait = await mgr.acquire("POST", "/tickle")
    assert cls == "auth_lifecycle"
    assert wait == 0.0


# ---- CP-Client-Hook: 429 setzt Backoff ----


async def test_cp_client_hooks_pacing_violation_on_429() -> None:
    mgr = ThrottleManager()
    base_url = "http://cpgateway:5000"

    async with httpx.AsyncClient(base_url=base_url) as raw_client:
        client = CPGatewayClient(base_url=base_url, throttle=mgr, http_client=raw_client)
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{base_url}/iserver/marketdata/snapshot").mock(
                return_value=httpx.Response(429, json={"error": "pacing-violation"})
            )
            response = await client.get("/iserver/marketdata/snapshot", params={"conids": "1"})
            assert response.status_code == 429

        # Klassifiziertes Bucket muss Backoff erhoeht haben.
        assert mgr.bucket("quotes_snapshot").extra_wait_s > 0.0
        assert mgr.metrics()["quotes_snapshot"]["pacing_violations_total"] == 1.0


async def test_cp_client_records_success_when_2xx() -> None:
    mgr = ThrottleManager()
    base_url = "http://cpgateway:5000"

    async with httpx.AsyncClient(base_url=base_url) as raw_client:
        client = CPGatewayClient(base_url=base_url, throttle=mgr, http_client=raw_client)
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{base_url}/iserver/secdef/search").mock(
                return_value=httpx.Response(200, json=[])
            )
            await client.get("/iserver/secdef/search", params={"symbol": "AAPL"})
        assert mgr.metrics()["instruments"]["acquired_total"] == 1.0
        # Kein Backoff aktiv.
        assert mgr.bucket("instruments").extra_wait_s == 0.0
