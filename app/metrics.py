"""
Shared rolling-window request metrics using diskcache.

Records are stored in a process-safe SQLite-backed cache shared across all
uvicorn worker processes.  Each record expires automatically after
WINDOW_SECONDS, so no manual trimming is needed.

Timestamps use time.time() (Unix wall clock) so they are comparable across
worker processes — unlike time.monotonic() which is relative to each
process's start time.
"""
from __future__ import annotations

import os
import statistics
import tempfile
import time
import uuid
from typing import Any

import diskcache

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
WINDOW_SECONDS: int = 3600   # rolling window length (1 hour)

# ---------------------------------------------------------------------------
# Storage — initialised by init_metrics() in the FastAPI lifespan
# ---------------------------------------------------------------------------
_metrics: diskcache.Cache | None = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def init_metrics(directory: str = "") -> None:
    """Open the shared metrics store.  Called once per worker at startup."""
    global _metrics
    cache_dir = directory or os.path.join(
        tempfile.gettempdir(), "volltextextraktion_metrics"
    )
    _metrics = diskcache.Cache(directory=cache_dir, size_limit=64 * 1024 * 1024)
    _metrics.expire()  # evict stale records left over from previous runs


def close_metrics() -> None:
    """Close the metrics store handle for this worker."""
    if _metrics is not None:
        _metrics.close()


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------
def record_request(
    mode: str,
    elapsed_s: float,
    *,
    success: bool,
    cached: bool,
    error_type: str | None = None,
) -> None:
    """Persist one record.  No-op when metrics store is not initialised."""
    if _metrics is None:
        return
    ts = time.time()
    record = (ts, mode, elapsed_s, success, cached, error_type)
    # Key must be unique across all processes; combine timestamp + pid + random suffix
    key = f"m:{ts:.6f}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    _metrics.set(key, record, expire=WINDOW_SECONDS, retry=True)


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------
def _current_window() -> list[tuple]:
    """Return all records within the last WINDOW_SECONDS from all workers."""
    if _metrics is None:
        return []
    cutoff = time.time() - WINDOW_SECONDS
    result = []
    for key in list(_metrics):
        v = _metrics.get(key)          # returns None if the item has since expired
        if v is not None and v[0] >= cutoff:
            result.append(v)
    return result


def get_window_stats(cache_obj: Any | None = None) -> dict:
    """Aggregate statistics over the shared rolling window (all workers combined).

    Args:
        cache_obj: Optional diskcache.Cache instance to read live entry count from.
    """
    rows = _current_window()

    total        = len(rows)
    errors       = [r for r in rows if not r[3]]
    successes    = [r for r in rows if r[3] and not r[4]]  # real fetches (not cached)
    cached_hits  = [r for r in rows if r[3] and r[4]]

    error_count  = len(errors)
    cached_count = len(cached_hits)

    # ── Latency (seconds) — real fetches only, not cache hits ────────────────
    def _latency(mode_filter: str | None) -> dict:
        times = [r[2] for r in successes if mode_filter is None or r[1] == mode_filter]
        if not times:
            return {"p50": None, "p95": None, "avg": None, "n": 0}
        s = sorted(times)
        return {
            "p50": round(statistics.median(s), 3),
            "p95": round(s[int(len(s) * 0.95)], 3),
            "avg": round(statistics.mean(s), 3),
            "n":   len(s),
        }

    # ── Throughput: requests per minute over the last 60 minutes ─────────────
    now = time.time()
    buckets: list[int] = [0] * 60
    for r in rows:
        age_min = int((now - r[0]) / 60)
        if 0 <= age_min < 60:
            buckets[59 - age_min] += 1  # index 59 = most recent minute

    # ── Errors by type ───────────────────────────────────────────────────────
    errors_by_type: dict[str, int] = {}
    for r in errors:
        etype = r[5] or "unknown"
        errors_by_type[etype] = errors_by_type.get(etype, 0) + 1

    # ── Cache entries from the live result-cache object ───────────────────────
    cache_entries = len(cache_obj) if cache_obj is not None else None

    return {
        "window_seconds": WINDOW_SECONDS,
        "requests_total": total,
        "requests_success": total - error_count,
        "requests_error": error_count,
        "error_rate_pct": round(error_count / total * 100, 1) if total else 0.0,
        "cache_hits": cached_count,
        "cache_hit_rate_pct": round(cached_count / total * 100, 1) if total else 0.0,
        "cache_entries_current": cache_entries,
        "latency_seconds": {
            "all":  _latency(None),
            "fast": _latency("fast"),
            "js":   _latency("js"),
            "auto": _latency("auto"),
        },
        "errors_by_type": errors_by_type,
        "throughput_per_minute": buckets,
    }
