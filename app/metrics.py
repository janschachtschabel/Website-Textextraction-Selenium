"""At most 60 minute buckets; stats cost does not grow with request volume."""

import math
import time

BOUNDS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, math.inf)
TOTALS = "metrics:total"


def _empty():
    return {
        "requests": 0,
        "errors": 0,
        "cached": 0,
        "coalesced": 0,
        "n": 0,
        "sum": 0.0,
        "max": 0.0,
        "histogram": [0] * len(BOUNDS),
    }


def _add(row, elapsed, success, cached, coalesced):
    row["requests"] += 1
    row["errors"] += not success
    row["cached"] += cached
    row["coalesced"] += coalesced
    if success and not cached and not coalesced:
        row["n"] += 1
        row["sum"] += elapsed
        row["max"] = max(row["max"], elapsed)
        row["histogram"][next(i for i, upper in enumerate(BOUNDS) if elapsed <= upper)] += 1


class Metrics:
    def __init__(self, store, run_sync):
        self.store, self.run_sync = store, run_sync

    async def record(self, elapsed, success, cached=False, coalesced=False):
        await self.run_sync(self._record, elapsed, success, cached, coalesced)

    def _record(self, elapsed, success, cached, coalesced):
        minute = int(time.time() / 60)
        key = f"metrics:{minute % 60}"
        with self.store.transact():
            bucket = self.store.get(key)
            if not bucket or bucket["minute"] != minute:
                bucket = {**_empty(), "minute": minute}
            # Cumulative since the store was created: Prometheus counters must never go down.
            totals = self.store.get(TOTALS) or _empty()
            for row in (bucket, totals):
                _add(row, elapsed, success, cached, coalesced)
            self.store.set(key, bucket, expire=3660)
            self.store.set(TOTALS, totals)

    async def totals(self):
        return await self.run_sync(self.store.get, TOTALS) or _empty()

    async def stats(self, cache_entries):
        return await self.run_sync(self._stats, cache_entries)

    def _stats(self, cache_entries):
        minute = int(time.time() / 60)
        total = _empty()
        throughput = [0] * 60
        for index in range(60):
            row = self.store.get(f"metrics:{index}")
            if not row or not 0 <= minute - row["minute"] < 60:
                continue
            throughput[59 - (minute - row["minute"])] = row["requests"]
            for name in ("requests", "errors", "cached", "coalesced", "n", "sum"):
                total[name] += row[name]
            total["max"] = max(total["max"], row["max"])
            total["histogram"] = [a + b for a, b in zip(total["histogram"], row["histogram"], strict=True)]

        def percentile(fraction):
            if not total["n"]:
                return None
            cumulative = 0
            for upper, count in zip(BOUNDS, total["histogram"], strict=True):
                cumulative += count
                if cumulative >= math.ceil(total["n"] * fraction):
                    return min(upper, total["max"])

        return {
            "window_seconds": 3600,
            "window_resolution_seconds": 60,
            "requests_total": total["requests"],
            "requests_success": total["requests"] - total["errors"],
            "requests_error": total["errors"],
            "cache_hits": total["cached"],
            "coalesced": total["coalesced"],
            "cache_entries_current": cache_entries,
            "latency_seconds": {
                "p50": percentile(0.5),
                "p95": percentile(0.95),
                "n": total["n"],
                "avg": total["sum"] / total["n"] if total["n"] else None,
                "approximate_percentiles": True,
            },
            "throughput_per_minute": throughput,
        }
