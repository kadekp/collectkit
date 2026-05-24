"""
Simple metrics collection for monitoring.

Provides in-memory counters and histograms that can be exposed via health endpoint.
Can be upgraded to StatsD/Prometheus client later if needed.

Usage:
    from .metrics import metrics
    
    metrics.increment("messages_sent", tags={"status": "success"})
    
    with metrics.timer("llm_latency"):
        # do LLM call
        pass
"""

import time
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any


class Metrics:
    """Thread-safe metrics collector."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: Dict[str, int] = {}
        self._histograms: Dict[str, List[float]] = {}
        self._start_time = datetime.now(timezone.utc)

    def increment(self, metric: str, value: int = 1, tags: Optional[Dict[str, str]] = None) -> None:
        """Increment a counter metric."""
        key = self._make_key(metric, tags)
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def record_duration(self, metric: str, duration_ms: float, tags: Optional[Dict[str, str]] = None) -> None:
        """Record a duration metric in milliseconds."""
        key = self._make_key(metric, tags)
        with self._lock:
            if key not in self._histograms:
                self._histograms[key] = []
            # Keep last 1000 values to prevent memory issues
            if len(self._histograms[key]) >= 1000:
                self._histograms[key] = self._histograms[key][-500:]
            self._histograms[key].append(duration_ms)

    @contextmanager
    def timer(self, metric: str, tags: Optional[Dict[str, str]] = None):
        """Context manager to time operations."""
        start = time.time()
        try:
            yield
        finally:
            duration_ms = (time.time() - start) * 1000
            self.record_duration(metric, duration_ms, tags)

    def get_counter(self, metric: str, tags: Optional[Dict[str, str]] = None) -> int:
        """Get current value of a counter."""
        key = self._make_key(metric, tags)
        with self._lock:
            return self._counters.get(key, 0)

    def get_histogram_stats(self, metric: str, tags: Optional[Dict[str, str]] = None) -> Dict[str, float]:
        """Get statistics for a histogram metric."""
        key = self._make_key(metric, tags)
        with self._lock:
            values = self._histograms.get(key, [])
            if not values:
                return {"count": 0, "min": 0, "max": 0, "avg": 0, "p50": 0, "p95": 0, "p99": 0}

            sorted_values = sorted(values)
            count = len(sorted_values)
            return {
                "count": count,
                "min": sorted_values[0],
                "max": sorted_values[-1],
                "avg": sum(sorted_values) / count,
                "p50": sorted_values[int(count * 0.5)],
                "p95": sorted_values[int(count * 0.95)] if count > 1 else sorted_values[-1],
                "p99": sorted_values[int(count * 0.99)] if count > 1 else sorted_values[-1],
            }

    def get_all(self) -> Dict[str, Any]:
        """Get all metrics for health endpoint."""
        with self._lock:
            histogram_stats = {}
            for key, values in self._histograms.items():
                if values:
                    sorted_values = sorted(values)
                    count = len(sorted_values)
                    histogram_stats[key] = {
                        "count": count,
                        "min": round(sorted_values[0], 2),
                        "max": round(sorted_values[-1], 2),
                        "avg": round(sum(sorted_values) / count, 2),
                    }

            return {
                "counters": dict(self._counters),
                "histograms": histogram_stats,
                "uptime_seconds": (datetime.now(timezone.utc) - self._start_time).total_seconds(),
            }

    def reset(self) -> None:
        """Reset all metrics (useful for testing)."""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()

    @staticmethod
    def _make_key(metric: str, tags: Optional[Dict[str, str]] = None) -> str:
        """Create a metric key with optional tags."""
        if not tags:
            return metric
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return f"{metric}{{{tag_str}}}"


# Global metrics instance
metrics = Metrics()
