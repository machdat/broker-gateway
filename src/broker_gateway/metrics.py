"""Prometheus-Metrics-Registry.

Single Source of Truth fuer alle Metriken. Module duerfen ausschliesslich
ueber dieses Modul Metriken registrieren oder updaten - keine ad-hoc-
Counter im Endpunkt-Code. Custom-Collector liest Live-Werte aus den
Singletons (AuthLifecycle, SubscriptionManager, ThrottleManager) bei
jedem Scrape.
"""
from __future__ import annotations

from typing import Iterator

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector


_DEFAULT_LATENCY_BUCKETS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)


class BrokerGatewayMetrics:
    """Container fuer alle Service-Metriken.

    Eine Instanz pro App-Lifespan. Tests koennen eine eigene Registry
    instanzieren, um die Standard-Registry nicht zu verschmutzen.
    """

    def __init__(self) -> None:
        self.registry = CollectorRegistry()

        self.requests_total = Counter(
            "broker_gateway_requests_total",
            "Anzahl HTTP-Requests an /v1.",
            labelnames=("path", "status", "scope"),
            registry=self.registry,
        )
        self.request_latency_seconds = Histogram(
            "broker_gateway_request_latency_seconds",
            "Latenz pro /v1-Request in Sekunden.",
            labelnames=("path",),
            buckets=_DEFAULT_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.pacing_violations_total = Counter(
            "broker_gateway_pacing_violations_total",
            "CP-Gateway-Pacing-Violations (HTTP 429).",
            labelnames=("class",),
            registry=self.registry,
        )

        # Gauges via Custom-Collector (Live-Werte beim Scrape).
        self._collector: _LiveCollector | None = None

    def attach_live_collector(self, collector: "_LiveCollector") -> None:
        if self._collector is not None:
            self.registry.unregister(self._collector)
        self._collector = collector
        self.registry.register(collector)

    def render(self) -> bytes:
        return generate_latest(self.registry)


class _LiveCollector(Collector):
    """Liest beim Scrape Live-Werte aus den Singletons."""

    def __init__(
        self,
        *,
        lifecycle_snapshot,  # Callable[[], LifecycleSnapshot]
        subscription_count,  # Callable[[], int]
        throttle_metrics,    # Callable[[], dict[ThrottleClass, dict[str, float]]]
    ) -> None:
        self._lifecycle_snapshot = lifecycle_snapshot
        self._subscription_count = subscription_count
        self._throttle_metrics = throttle_metrics

    def collect(self) -> Iterator[GaugeMetricFamily]:
        snap = self._lifecycle_snapshot()
        session_age = GaugeMetricFamily(
            "broker_gateway_session_age_seconds",
            "Alter der aktuellen IBKR-Session in Sekunden (None -> -1).",
        )
        session_age.add_metric([], snap.session_age_s if snap.session_age_s is not None else -1.0)
        yield session_age

        sub_count = GaugeMetricFamily(
            "broker_gateway_subscription_count",
            "Aktive CP-Gateway-Subscriptions (conids).",
        )
        sub_count.add_metric([], float(self._subscription_count()))
        yield sub_count

        throttle = GaugeMetricFamily(
            "broker_gateway_throttle_extra_wait_seconds",
            "Aktueller Backoff (extra_wait_s) pro Throttle-Klasse.",
            labels=("class",),
        )
        for cls, data in self._throttle_metrics().items():
            throttle.add_metric([cls], float(data.get("extra_wait_s", 0.0)))
        yield throttle


def make_live_collector(
    *,
    lifecycle_snapshot,
    subscription_count,
    throttle_metrics,
) -> _LiveCollector:
    return _LiveCollector(
        lifecycle_snapshot=lifecycle_snapshot,
        subscription_count=subscription_count,
        throttle_metrics=throttle_metrics,
    )


def get_metrics() -> BrokerGatewayMetrics:
    raise RuntimeError(
        "get_metrics muss in der App per dependency_overrides gesetzt werden"
    )


__all__ = [
    "BrokerGatewayMetrics",
    "get_metrics",
    "make_live_collector",
]
