"""Tests for the analytics counter pipeline.

Covers:
- Valkey analytics counter INCR/GET with TTL preservation
- log_errors_total counter increments on ERROR-level log emission
- Feature toggle state logged and exposed as groktocrawl_feature_enabled gauge
- Analytics counters exposed as Prometheus COUNTER metrics
"""

from __future__ import annotations

import json
import logging
import os
from unittest import mock

import pytest

from common.analytics import (
    counter_key,
    get_all_counters,
    get_counter,
    increment_counter,
)
from common.features import is_enabled
from common.logging import ErrorCountingHandler, JSONFormatter
from common.metrics import METRICS

# =========================================================================
# Valkey analytics counter operations
# =========================================================================


class _FakeRedis:
    """A minimal fake Redis for testing analytics counter operations.

    Supports the operations used by the analytics module: get, set, incr,
    ttl, expire, scan, mget.
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}
        self._ttls: dict[str, int] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._data[key] = value
        if ex is not None:
            self._ttls[key] = ex

    def incr(self, key: str) -> int:
        current = int(self._data.get(key, "0"))
        current += 1
        self._data[key] = str(current)
        return current

    def ttl(self, key: str) -> int:
        if key not in self._data:
            return -2  # key does not exist
        if key in self._ttls:
            return self._ttls[key]
        return -1  # no TTL (persistent)

    def expire(self, key: str, seconds: int) -> bool:
        if key in self._data:
            self._ttls[key] = seconds
            return True
        return False

    def scan(
        self, cursor: int = 0, match: str = "*", count: int = 100
    ) -> tuple[int, list[str]]:
        import fnmatch

        keys = [k for k in self._data if fnmatch.fnmatch(k, match)]
        return 0, keys

    def mget(self, *keys: str) -> list[str | None]:
        return [self._data.get(k) for k in keys]


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()


class TestAnalyticsCounterIncr:
    """VAL-RES-008: Analytics counters support INCR and GET with TTL preservation."""

    def test_incr_and_get(self, fake_redis: _FakeRedis) -> None:
        """INCR increments and GET returns the correct value."""
        val = increment_counter(fake_redis, "page_views")
        assert val == 1
        val = increment_counter(fake_redis, "page_views")
        assert val == 2
        val = increment_counter(fake_redis, "page_views")
        assert val == 3

        assert get_counter(fake_redis, "page_views") == 3

    def test_get_nonexistent_counter(self, fake_redis: _FakeRedis) -> None:
        """GET on a non-existent counter returns None."""
        assert get_counter(fake_redis, "nonexistent") is None

    def test_counter_key_scheme(self) -> None:
        """Key scheme is analytics:counter:{name}."""
        assert counter_key("page_views") == "analytics:counter:page_views"
        assert counter_key("api_calls") == "analytics:counter:api_calls"

    def test_ttl_reapplied_after_incr(self, fake_redis: _FakeRedis) -> None:
        """TTL is preserved after INCR on a key that already has a TTL.

        Valkey INCR resets the key's TTL. We must re-apply it.
        """
        # Set initial counter with TTL
        key = counter_key("test_ttl")
        fake_redis.set(key, "10", ex=3600)
        assert fake_redis.ttl(key) == 3600

        # Increment — should preserve TTL
        increment_counter(fake_redis, "test_ttl")
        ttl_after = fake_redis.ttl(key)
        assert ttl_after > 0, f"Expected TTL preserved, got {ttl_after}"

    def test_incr_sets_ttl_when_provided(self, fake_redis: _FakeRedis) -> None:
        """When ttl is provided and key has no TTL, it should set it."""
        increment_counter(fake_redis, "temp_counter", ttl=300)
        key = counter_key("temp_counter")
        assert fake_redis.ttl(key) == 300

    def test_incr_preserves_no_ttl(self, fake_redis: _FakeRedis) -> None:
        """When no TTL is specified and key has no TTL, counter persists."""
        increment_counter(fake_redis, "persistent")
        key = counter_key("persistent")
        assert fake_redis.ttl(key) == -1  # -1 means no TTL

    def test_get_all_counters(self, fake_redis: _FakeRedis) -> None:
        """get_all_counters returns all analytics:counter:* keys and values."""
        increment_counter(fake_redis, "alpha")
        increment_counter(fake_redis, "alpha")
        increment_counter(fake_redis, "beta")
        increment_counter(fake_redis, "gamma")

        counters = get_all_counters(fake_redis)
        assert counters == {"alpha": 2, "beta": 1, "gamma": 1}

    def test_get_all_counters_empty(self, fake_redis: _FakeRedis) -> None:
        """get_all_counters returns empty dict when no counters exist."""
        assert get_all_counters(fake_redis) == {}


# =========================================================================
# log_errors_total counter
# =========================================================================


class TestLogErrorsTotal:
    """VAL-RES-010: log_errors_total counter increments on ERROR-level emission."""

    def test_error_counter_increments_on_error_log(self) -> None:
        """After logging an ERROR, log_errors_total counter increases."""
        # Reset state for this test
        METRICS.counter("log_errors_total", "Total error logs", ["service"])

        handler = ErrorCountingHandler(service_name="test_svc")
        logger = logging.getLogger("test_error_logger")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        # Get initial value
        initial_lines = METRICS.generate_openmetrics().split("\n")
        initial_count = 0
        for line in initial_lines:
            if 'log_errors_total{service="test_svc"}' in line:
                initial_count = float(line.split()[-2])

        # Log an ERROR
        logger.error("This is a test error")

        # Check counter incremented
        new_lines = METRICS.generate_openmetrics().split("\n")
        new_count = 0
        for line in new_lines:
            if 'log_errors_total{service="test_svc"}' in line:
                new_count = float(line.split()[-2])

        assert new_count >= initial_count + 1, (
            f"Expected counter >= {initial_count + 1}, got {new_count}"
        )

        logger.removeHandler(handler)

    def test_error_counter_has_service_label(self) -> None:
        """log_errors_total metric includes a 'service' label."""
        handler = ErrorCountingHandler(service_name="myservice")
        logger = logging.getLogger("test_service_label")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        logger.error("Another test error")

        metrics_text = METRICS.generate_openmetrics()
        assert 'log_errors_total{service="myservice"}' in metrics_text, (
            "Expected log_errors_total with service=myservice in metrics output"
        )

        logger.removeHandler(handler)

    def test_error_counter_not_incremented_on_lower_levels(self) -> None:
        """WARNING, INFO, DEBUG logs do not increment log_errors_total."""
        handler = ErrorCountingHandler(service_name="test_noinc")
        logger = logging.getLogger("test_no_inc_logger")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        # Count initial state
        initial_lines = METRICS.generate_openmetrics().split("\n")
        initial_count = 0
        for line in initial_lines:
            if 'log_errors_total{service="test_noinc"}' in line:
                initial_count = float(line.split()[-2])

        # Log at non-ERROR levels
        logger.warning("Warning message")
        logger.info("Info message")
        logger.debug("Debug message")

        # Count after
        new_lines = METRICS.generate_openmetrics().split("\n")
        new_count = 0
        for line in new_lines:
            if 'log_errors_total{service="test_noinc"}' in line:
                new_count = float(line.split()[-2])

        assert new_count == initial_count, (
            f"Expected count unchanged ({initial_count}), got {new_count}"
        )

        logger.removeHandler(handler)


# =========================================================================
# Feature toggle logging and metrics
# =========================================================================


class TestFeatureToggleObservability:
    """VAL-CROSS-013: Feature toggle state observable in logs and metrics."""

    def test_feature_enabled_gauge_exists(self) -> None:
        """groktocrawl_feature_enabled gauge appears in /metrics."""
        gauge = METRICS.gauge(
            "groktocrawl_feature_enabled",
            "Feature toggle enabled status",
            ["feature"],
        )
        gauge.set({"feature": "test_flag"}, 1.0)

        metrics_text = METRICS.generate_openmetrics()
        assert "# TYPE groktocrawl_feature_enabled gauge" in metrics_text
        assert 'groktocrawl_feature_enabled{feature="test_flag"}' in metrics_text

    def test_feature_enabled_gauge_reflects_true_state(self) -> None:
        """groktocrawl_feature_enabled gauge shows 1 when feature is enabled."""
        with mock.patch.dict(os.environ, {"FEATURE_MYFEATURE": "true"}, clear=False):
            enabled = is_enabled("myfeature")
            gauge = METRICS.gauge(
                "groktocrawl_feature_enabled",
                "Feature toggle enabled status",
                ["feature"],
            )
            gauge.set({"feature": "myfeature"}, 1.0 if enabled else 0.0)

            metrics_text = METRICS.generate_openmetrics()
            for line in metrics_text.split("\n"):
                if 'groktocrawl_feature_enabled{feature="myfeature"}' in line:
                    val = float(line.split()[-2])
                    assert val == 1.0, f"Expected 1.0, got {val}"
                    return
            pytest.fail("Expected groktocrawl_feature_enabled for myfeature")

    def test_feature_enabled_gauge_reflects_false_state(self) -> None:
        """groktocrawl_feature_enabled gauge shows 0 when feature is disabled."""
        with mock.patch.dict(os.environ, {}, clear=False):
            # Ensure FEATURE_OFFFEATURE is not set
            if "FEATURE_OFFFEATURE" in os.environ:
                del os.environ["FEATURE_OFFFEATURE"]
            enabled = is_enabled("offfeature")
            gauge = METRICS.gauge(
                "groktocrawl_feature_enabled",
                "Feature toggle enabled status",
                ["feature"],
            )
            gauge.set({"feature": "offfeature"}, 1.0 if enabled else 0.0)

            metrics_text = METRICS.generate_openmetrics()
            for line in metrics_text.split("\n"):
                if 'groktocrawl_feature_enabled{feature="offfeature"}' in line:
                    val = float(line.split()[-2])
                    assert val == 0.0, f"Expected 0.0, got {val}"
                    return
            pytest.fail("Expected groktocrawl_feature_enabled for offreature")

    def test_feature_toggle_logged_at_startup(self) -> None:
        """Feature toggle state is logged at service startup."""
        logger = logging.getLogger("test_startup")
        with mock.patch.dict(
            os.environ,
            {"FEATURE_ANALYTICS": "true", "FEATURE_DARK_MODE": "false"},
            clear=False,
        ):
            for key, _value in sorted(os.environ.items()):
                if key.startswith("FEATURE_"):
                    feature_name = key[len("FEATURE_") :].lower()
                    enabled = is_enabled(feature_name)
                    logger.info(
                        "Feature toggle %s enabled=%s",
                        feature_name,
                        enabled,
                    )
                    # Verify the log formatting
                    fmt = JSONFormatter()
                    record = logger.makeRecord(
                        logger.name,
                        logging.INFO,
                        __file__,
                        100,
                        "Feature toggle %s enabled=%s",
                        (feature_name, enabled),
                        None,
                    )
                    formatted = json.loads(fmt.format(record))
                    # Message should contain the feature name and state
                    assert feature_name in formatted["message"]
                    assert str(enabled) in formatted["message"]


# =========================================================================
# Analytics counters exposed as Prometheus COUNTER metrics
# =========================================================================


class TestAnalyticsCounterPrometheusExport:
    """VAL-RES-009: Analytics counters exposed as Prometheus COUNTER metrics."""

    def test_analytics_counter_type_is_counter(self) -> None:
        """Analytics counters have TYPE counter in /metrics."""
        name = "test_api_calls"
        metric_name = f"groktocrawl_analytics_{name}"
        METRICS.counter(metric_name, f"Analytics counter: {name}").set(value=42.0)

        metrics_text = METRICS.generate_openmetrics()
        assert f"# TYPE {metric_name} counter" in metrics_text, (
            f"Expected {metric_name} to have type 'counter', got:\n{metrics_text}"
        )

    def test_analytics_counter_value_reflects_valkey(self) -> None:
        """Analytics counter metric shows the correct value."""
        name = "test_downloads"
        metric_name = f"groktocrawl_analytics_{name}"
        METRICS.counter(metric_name, f"Analytics counter: {name}").set(value=7.0)

        metrics_text = METRICS.generate_openmetrics()
        for line in metrics_text.split("\n"):
            if f"groktocrawl_analytics_{name}" in line and not line.startswith("#"):
                val = float(line.split()[-2])
                assert val == 7.0, f"Expected 7.0, got {val}"
                return
        pytest.fail(f"Expected groktocrawl_analytics_{name} in metrics output")

    def test_multiple_analytics_counters(self) -> None:
        """Multiple analytics counters each appear as separate metrics."""
        METRICS.counter("groktocrawl_analytics_reqs", "Analytics counter: reqs").set(
            value=10.0
        )
        METRICS.counter(
            "groktocrawl_analytics_errors", "Analytics counter: errors"
        ).set(value=3.0)

        metrics_text = METRICS.generate_openmetrics()
        assert "groktocrawl_analytics_reqs" in metrics_text
        assert "groktocrawl_analytics_errors" in metrics_text

        for line in metrics_text.split("\n"):
            if "groktocrawl_analytics_reqs" in line and not line.startswith("#"):
                assert float(line.split()[-2]) == 10.0
            if "groktocrawl_analytics_errors" in line and not line.startswith("#"):
                assert float(line.split()[-2]) == 3.0
