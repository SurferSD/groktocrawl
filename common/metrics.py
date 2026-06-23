"""In-memory metrics collector with OpenMetrics text export.

Provides Counter, Histogram, and Gauge primitives backed by plain Python
data structures. Thread-safe via ``threading.Lock``. Generates OpenMetrics
text format for Prometheus consumption — no external dependencies.

Shared by all GroktoCrawl services. Import in each service's app factory
and wire the /metrics endpoint.

Usage::

    from common.metrics import METRICS

    # Counter — how many things happened
    METRICS.counter("requests_total", "Total requests", ["endpoint"]).inc({"endpoint": "embed"})

    # Histogram — latency distribution
    METRICS.histogram("latency_seconds", "Request latency", ["endpoint"], buckets=DEFAULT_BUCKETS).observe({"endpoint": "search_vector"}, 0.042)

    # Gauge — current value
    METRICS.gauge("docs_total", "Current document count").set(250000)

    # Export
    print(METRICS.generate_openmetrics())
"""

import threading
import time
from collections import defaultdict

DEFAULT_BUCKETS = [
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
    30.0,
    60.0,
]


class _SafeCounter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple, float] = defaultdict(float)

    def inc(self, labels: dict[str, str] | None = None, value: float = 1.0) -> None:
        key = tuple(sorted(labels.items())) if labels else ()
        with self._lock:
            self._data[key] += value

    def set(self, labels: dict[str, str] | None = None, value: float = 0.0) -> None:
        """Set the counter to an absolute value (replaces any prior value).

        Primarily used by the analytics counter exporter to report the
        current Valkey counter value as a monotonically-increasing counter.

        Args:
            labels: Optional label key/value pairs.
            value: The absolute value to set.
        """
        key = tuple(sorted(labels.items())) if labels else ()
        with self._lock:
            self._data[key] = value

    def _collect(self) -> list[tuple[tuple, float]]:
        with self._lock:
            return list(self._data.items())


class _SafeHistogram:
    def __init__(self, buckets: list[float]) -> None:
        self._buckets = sorted(buckets)
        self._lock = threading.Lock()
        self._counts: dict[float, defaultdict[tuple, int]] = {
            b: defaultdict(int) for b in self._buckets
        }
        self._sums: defaultdict[tuple, float] = defaultdict(float)
        self._totals: defaultdict[tuple, int] = defaultdict(int)

    def observe(self, labels: dict[str, str] | None = None, value: float = 0.0) -> None:
        key = tuple(sorted(labels.items())) if labels else ()
        with self._lock:
            self._totals[key] += 1
            self._sums[key] += value
            for b in self._buckets:
                if value <= b:
                    self._counts[b][key] += 1

    def _collect(
        self,
    ) -> list[tuple[tuple, float, float, dict[float, int]]]:
        with self._lock:
            result: list[tuple[tuple, float, float, dict[float, int]]] = []
            for key in self._totals:
                bucket_map = {b: self._counts[b].get(key, 0) for b in self._buckets}
                result.append(
                    (key, self._sums[key], float(self._totals[key]), bucket_map)
                )
            return result


class _SafeGauge:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple, float] = defaultdict(float)

    def set(self, labels: dict[str, str] | None = None, value: float = 0.0) -> None:
        key = tuple(sorted(labels.items())) if labels else ()
        with self._lock:
            self._data[key] = value

    def _collect(self) -> list[tuple[tuple, float]]:
        with self._lock:
            return list(self._data.items())


class _MetricFamily:
    def __init__(
        self,
        name: str,
        help_text: str,
        metric_type: str,
        buckets: list[float] | None = None,
    ) -> None:
        self.name = name
        self.help = help_text
        self.metric_type = metric_type
        self.buckets = buckets
        self._created = time.time()


class MetricsCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, tuple[_MetricFamily, _SafeCounter]] = {}
        self._histograms: dict[str, tuple[_MetricFamily, _SafeHistogram]] = {}
        self._gauges: dict[str, tuple[_MetricFamily, _SafeGauge]] = {}

    def counter(
        self, name: str, help_text: str, label_names: list[str] | None = None
    ) -> _SafeCounter:
        with self._lock:
            if name in self._counters:
                return self._counters[name][1]
            family = _MetricFamily(name, help_text, "counter")
            counter = _SafeCounter()
            self._counters[name] = (family, counter)
            return counter

    def histogram(
        self,
        name: str,
        help_text: str,
        label_names: list[str] | None = None,
        buckets: list[float] | None = None,
    ) -> _SafeHistogram:
        with self._lock:
            if name in self._histograms:
                return self._histograms[name][1]
            buckets = buckets or DEFAULT_BUCKETS
            family = _MetricFamily(name, help_text, "histogram", buckets=buckets)
            histogram = _SafeHistogram(buckets)
            self._histograms[name] = (family, histogram)
            return histogram

    def gauge(
        self, name: str, help_text: str, label_names: list[str] | None = None
    ) -> _SafeGauge:
        with self._lock:
            if name in self._gauges:
                return self._gauges[name][1]
            family = _MetricFamily(name, help_text, "gauge")
            gauge = _SafeGauge()
            self._gauges[name] = (family, gauge)
            return gauge

    def generate_openmetrics(self) -> str:
        lines: list[str] = []
        now_ms = int(time.time() * 1000)

        with self._lock:
            for family, counter in self._counters.values():
                lines.append(f"# HELP {family.name} {family.help}")
                lines.append(f"# TYPE {family.name} counter")
                for key, val in counter._collect():
                    label_str = _format_labels(key)
                    lines.append(f"{family.name}{label_str} {val} {now_ms}")

            for family, histogram in self._histograms.values():
                lines.append(f"# HELP {family.name} {family.help}")
                lines.append(f"# TYPE {family.name} histogram")
                for key, sum_val, count, bucket_map in histogram._collect():
                    label_str = _format_labels(key)
                    lines.append(f"{family.name}_sum{label_str} {sum_val} {now_ms}")
                    lines.append(f"{family.name}_count{label_str} {count} {now_ms}")
                    for b, bcount in bucket_map.items():
                        ble = _format_labels(key, {"le": str(b)})
                        lines.append(f"{family.name}_bucket{ble} {bcount} {now_ms}")
                    lines.append(
                        f"{family.name}_bucket{_format_labels(key, {'le': '+Inf'})} {count} {now_ms}"
                    )

            for family, gauge in self._gauges.values():
                lines.append(f"# HELP {family.name} {family.help}")
                lines.append(f"# TYPE {family.name} gauge")
                for key, val in gauge._collect():
                    label_str = _format_labels(key)
                    lines.append(f"{family.name}{label_str} {val} {now_ms}")

        lines.append("# EOF")
        return "\n".join(lines) + "\n"


def _format_labels(base: tuple, extra: dict[str, str] | None = None) -> str:
    parts: list[str] = []
    for k, v in base:
        parts.append(f'{k}="{v}"')
    if extra:
        for k, v in extra.items():
            parts.append(f'{k}="{v}"')
    if not parts:
        return ""
    return "{" + ",".join(parts) + "}"


METRICS = MetricsCollector()
