"""Prometheus exposition of the shared metrics, rendered per scrape."""

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily

from .metrics import BOUNDS

__all__ = ["CONTENT_TYPE_LATEST", "render"]


class _Snapshot:
    """Collector over values read once per scrape; counters come from the shared store."""

    def __init__(self, totals: dict, gauges: dict):
        self.totals, self.gauges = totals, gauges

    def collect(self):
        totals = self.totals
        requests = CounterMetricFamily("extraction_requests", "Crawl requests by outcome", labels=["outcome"])
        requests.add_metric(["success"], totals["requests"] - totals["errors"])
        requests.add_metric(["error"], totals["errors"])
        yield requests
        yield CounterMetricFamily("extraction_cache_hits", "Results served from the result cache", totals["cached"])
        yield CounterMetricFamily(
            "extraction_coalesced", "Results shared with an identical request", totals["coalesced"]
        )
        cumulative, buckets = 0, []
        for upper, count in zip(BOUNDS, totals["histogram"], strict=True):
            cumulative += count
            buckets.append(("+Inf" if upper == float("inf") else str(upper), cumulative))
        yield HistogramMetricFamily(
            "extraction_request_duration_seconds",
            "Duration of successful extractions that were neither cached nor coalesced",
            buckets=buckets,
            sum_value=totals["sum"],
        )
        for name, (documentation, value) in self.gauges.items():
            if isinstance(value, dict):
                gauge = GaugeMetricFamily(name, documentation, labels=["pool", "state"])
                for (pool, state), sample in value.items():
                    gauge.add_metric([pool, state], sample)
                yield gauge
            else:
                yield GaugeMetricFamily(name, documentation, value)


def render(totals: dict, gauges: dict) -> bytes:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_Snapshot(totals, gauges))
    return generate_latest(registry)
