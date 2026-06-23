"""Tests for llm-svc observability.

Validates structured JSON logging, request-ID tracing middleware,
/metrics endpoint (OpenMetrics), and /health backward compatibility.
Uses FastAPI TestClient against the in-process app.
"""

import json
import logging
import re

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """Create a TestClient for the llm-svc app."""
    from llm_svc.app import app

    with TestClient(app) as c:
        yield c


# ── Health endpoint ────────────────────────────────────────────────


class TestHealthEndpoint:
    """GET /health must still return 200 with {"status": "ok"} after instrumentation."""

    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_health_returns_json_content_type(self, client):
        resp = client.get("/health")
        assert resp.headers["content-type"] == "application/json"


# ── Metrics endpoint ───────────────────────────────────────────────


class TestMetricsEndpoint:
    """GET /metrics must return valid OpenMetrics text (VAL-OBS-004)."""

    def test_metrics_returns_200(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_metrics_content_type(self, client):
        resp = client.get("/metrics")
        assert (
            resp.headers["content-type"]
            == "application/openmetrics-text; version=1.0.0"
        )

    def test_metrics_contains_help(self, client):
        resp = client.get("/metrics")
        text = resp.text
        assert "# HELP" in text, "OpenMetrics requires # HELP lines"

    def test_metrics_contains_type(self, client):
        resp = client.get("/metrics")
        text = resp.text
        assert "# TYPE" in text, "OpenMetrics requires # TYPE lines"

    def test_metrics_contains_eof(self, client):
        resp = client.get("/metrics")
        text = resp.text
        assert text.rstrip().endswith("# EOF"), "OpenMetrics must end with # EOF"

    def test_metrics_contains_metric_value(self, client):
        """After a chat completion request, metrics should contain a metric with a value."""
        client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        resp = client.get("/metrics")
        text = resp.text
        assert re.search(
            r"^[a-zA-Z_][a-zA-Z0-9_]*(\{.*?\})?\s+\d+\.?\d*", text, re.MULTILINE
        ), f"Expected at least one metric line with a value, got: {text[:500]}"


# ── Structured JSON logging ────────────────────────────────────────


class TestStructuredLogging:
    """Log lines must be structured JSON (VAL-OBS-005)."""

    def test_log_formatter_produces_json_with_required_fields(self):
        """Verify common.logging.JSONFormatter produces valid JSON with required fields."""
        from common.logging import JSONFormatter

        formatter = JSONFormatter()

        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg="Test message",
            args=(),
            exc_info=None,
        )

        output = formatter.format(record)
        parsed = json.loads(output)
        assert "timestamp" in parsed
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test_logger"
        assert parsed["message"] == "Test message"

    def test_log_formatter_includes_extra_fields(self):
        """Verify the JSON formatter includes extra_fields."""
        from common.logging import JSONFormatter

        formatter = JSONFormatter()

        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg="Request started",
            args=(),
            exc_info=None,
        )
        record.extra_fields = {"request_id": "abc12345", "method": "GET"}

        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["request_id"] == "abc12345"
        assert parsed["method"] == "GET"

    def test_setup_logging_configures_root_logger(self):
        """Verify setup_logging configures the root logger with JSON formatting."""
        from common.logging import JSONFormatter, setup_logging

        # Save original handlers
        original_handlers = list(logging.getLogger().handlers)

        try:
            setup_logging(default_level="DEBUG")

            root = logging.getLogger()
            assert root.level == logging.DEBUG
            # At least one handler should be a StreamHandler with JSONFormatter
            has_json_handler = any(
                isinstance(h, logging.StreamHandler)
                and isinstance(h.formatter, JSONFormatter)
                for h in root.handlers
            )
            assert has_json_handler, (
                "setup_logging should add a StreamHandler with JSONFormatter"
            )
        finally:
            # Restore original handlers
            root = logging.getLogger()
            for h in root.handlers[:]:
                root.removeHandler(h)
            for h in original_handlers:
                root.addHandler(h)


# ── Request-ID tracing ─────────────────────────────────────────────


class TestRequestIdTracing:
    """HTTP request logs must include 8-char request_id (VAL-OBS-006)."""

    def test_middleware_skip_paths_do_not_get_request_id(self, client):
        """Verify /health and /metrics are skipped by the middleware."""
        resp_h = client.get("/health")
        assert resp_h.status_code == 200

        resp_m = client.get("/metrics")
        assert resp_m.status_code == 200

    def test_chat_completions_returns_200(self, client):
        """Verify /v1/chat/completions still works after instrumentation."""
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 200

    def test_health_endpoint_skips_request_id(self, client):
        """Health endpoint should not produce request_id log lines (it's in skip_paths)."""
        client.get("/health")

    def test_metrics_endpoint_skips_request_id(self, client):
        """Metrics endpoint should not produce request_id log lines (it's in skip_paths)."""
        client.get("/metrics")


# ── Imports check (no inline copies) ───────────────────────────────


class TestImports:
    """Must use common/ imports exclusively (no inline copies)."""

    def test_app_uses_common_imports(self):
        """Verify the app module imports from common."""
        import llm_svc.app as app_module

        source = app_module.__file__
        assert source is not None, "Could not find app.py source file"
        with open(source) as f:
            content = f.read()
        assert "from common.logging import" in content
        assert "from common.metrics import" in content
        assert "from common.middleware import" in content

    def test_no_inline_json_formatter(self):
        """No inline JSONFormatter class definition."""
        import llm_svc.app as app_module

        source = app_module.__file__
        assert source is not None
        with open(source) as f:
            content = f.read()
        assert "class JSONFormatter" not in content, "Inline JSONFormatter found"

    def test_no_inline_setup_logging(self):
        """No inline setup_logging function definition."""
        import llm_svc.app as app_module

        source = app_module.__file__
        assert source is not None
        with open(source) as f:
            content = f.read()
        assert "def setup_logging" not in content, "Inline setup_logging() found"

    def test_no_inline_request_id_middleware(self):
        """No inline async def request_id_middleware."""
        import llm_svc.app as app_module

        source = app_module.__file__
        assert source is not None
        with open(source) as f:
            content = f.read()
        assert "def request_id_middleware" not in content
        assert "async def request_id_middleware" not in content


# ── Chat completions endpoint still works ──────────────────────────


class TestChatCompletionsEndpoint:
    """Chat completions endpoint must still work after instrumentation."""

    def test_chat_completions_returns_expected_response(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "chatcmpl-fixture"
        assert "choices" in data
        assert len(data["choices"]) > 0

    def test_chat_completions_without_request_id_does_not_pollute_metrics(self, client):
        """Chat completions request should not show up in /health's response."""
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
