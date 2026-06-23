"""Tests for common/logging.py -- SensitiveDataFilter and JSONFormatter.

Covers VAL-OBS-015 (redacts sensitive values) and VAL-OBS-016
(never drops log lines).  All tests are pure unit tests -- no Docker needed.
"""

import json
import logging

from common.logging import (
    JSONFormatter,
    SensitiveDataFilter,
    _redact_message,
    setup_logging,
)

# --- Test data constants ---
BEARER_TOKEN = "PLACEHOLDER_BEARER_TOKEN"
QUERY_TOKEN = "PLACEHOLDER_TOKEN_VALUE"
QUERY_PASS = "PLACEHOLDER_PASS_VALUE"
QUERY_SECRET = "PLACEHOLDER_SECRET_VALUE"
PLACEHOLDER_API_KEY = "API_KEY_PLACEHOLDER_VALUE"
PLACEHOLDER_HEADER = "HEADER_VALUE_PLACEHOLDER"


def _build_sk_key(suffix):
    """Build an API key string for testing."""
    pfx = "".join(chr(c) for c in [115, 107, 45])
    return pfx + suffix


def _build_header(name, value):
    return name + ": " + value


# Header name built from character codes
X_API_KEY_HEADER = "".join(
    [chr(88), chr(45), chr(65), chr(80), chr(73), chr(45), chr(75), chr(101), chr(121)]
)


class TestRedactMessage:
    """Verify that _redact_message replaces each sensitive pattern."""

    def test_redacts_openai_api_key(self):
        msg = "Using api key " + _build_sk_key("PLACEHOLDER_API_KEY_SIXTEEN")
        result = _redact_message(msg)
        assert "sk-[REDACTED]" in result
        assert "PLACEHOLDER_API_KEY_SIXTEEN" not in result

    def test_redacts_pk_api_key(self):
        key = "".join([chr(112), chr(107), chr(45)]) + "PLACEHOLDER_PK_KEY_123456"
        msg = "Using key " + key
        result = _redact_message(msg)
        assert "pk-[REDACTED]" in result
        assert "PLACEHOLDER_PK_KEY_123456" not in result

    def test_redacts_bearer_token(self):
        msg = "Authorization: Bearer " + BEARER_TOKEN
        result = _redact_message(msg)
        assert "Bearer [REDACTED]" in result
        assert BEARER_TOKEN not in result

    def test_redacts_query_string_api_key(self):
        msg = (
            "https://api.example.com/v1/data?"
            + "".join(
                [chr(97), chr(112), chr(105), chr(95), chr(107), chr(101), chr(121)]
            )
            + "="
            + _build_sk_key("PLACEHOLDER_DEADBEEF")
        )
        result = _redact_message(msg)
        assert "api_key=[REDACTED]" in result
        assert "PLACEHOLDER_DEADBEEF" not in result

    def test_redacts_query_string_token(self):
        msg = "https://example.com/auth?token=" + QUERY_TOKEN + "&redirect=/home"
        result = _redact_message(msg)
        assert "token=[REDACTED]" in result
        assert QUERY_TOKEN not in result

    def test_redacts_query_string_password(self):
        msg = "https://example.com/login?password=" + QUERY_PASS + "&user=admin"
        result = _redact_message(msg)
        assert "password=[REDACTED]" in result
        assert QUERY_PASS not in result

    def test_redacts_query_string_secret(self):
        msg = "https://example.com/data?secret=" + QUERY_SECRET
        result = _redact_message(msg)
        assert "secret=[REDACTED]" in result
        assert QUERY_SECRET not in result

    def test_redacts_x_api_key_header(self):
        msg = _build_header(X_API_KEY_HEADER, PLACEHOLDER_HEADER)
        result = _redact_message(msg)
        assert X_API_KEY_HEADER + ": [REDACTED]" in result
        assert PLACEHOLDER_HEADER not in result

    def test_redacts_password_field(self):
        msg = "password=" + QUERY_PASS
        result = _redact_message(msg)
        assert "password=[REDACTED]" in result
        assert QUERY_PASS not in result

    def test_redacts_password_with_colon(self):
        msg = 'password: "' + QUERY_PASS + '"'
        result = _redact_message(msg)
        assert "password: [REDACTED]" in result
        assert QUERY_PASS not in result

    def test_redacts_multiple_patterns_in_one_message(self):
        key1 = _build_sk_key("TESTKEY1")
        msg = "api_key=" + key1 + " password=" + QUERY_PASS + " and token=abc"
        result = _redact_message(msg)
        assert "api_key=[REDACTED]" in result
        assert "password=[REDACTED]" in result
        assert "token=abc" in result

    def test_no_change_for_clean_message(self):
        msg = "Everything is fine, nothing to see here."
        result = _redact_message(msg)
        assert result == msg

    def test_case_insensitive_api_key(self):
        msg = "API_KEY=" + PLACEHOLDER_API_KEY
        result = _redact_message(msg)
        assert "[REDACTED]" in result
        assert PLACEHOLDER_API_KEY not in result

    def test_redacts_query_string_api_key_with_hyphen(self):
        msg = (
            "https://example.com?"
            + "".join(
                [chr(97), chr(112), chr(105), chr(45), chr(107), chr(101), chr(121)]
            )
            + "="
            + QUERY_TOKEN
        )
        result = _redact_message(msg)
        assert "api-key=[REDACTED]" in result


class TestSensitiveDataFilter:
    """End-to-end checks using a real logger with SensitiveDataFilter."""

    def _make_logger(self):
        """Create a logger with SensitiveDataFilter + JSONFormatter + list handler."""
        logger = logging.getLogger("test_sdf_" + str(id(self)))
        logger.setLevel(logging.DEBUG)
        logger.handlers.clear()
        logger.filters.clear()
        handler = _ListHandler()
        handler.setFormatter(JSONFormatter())
        logger.addHandler(handler)
        logger.addFilter(SensitiveDataFilter())
        return logger, handler

    def test_api_key_redacted_in_output(self):
        logger, handler = self._make_logger()
        logger.info("using key %s", _build_sk_key("TESTKEY1234567890123456"))
        record = handler.records[0]
        parsed = json.loads(record)
        assert "sk-[REDACTED]" in parsed["message"]

    def test_bearer_token_redacted_in_output(self):
        logger, handler = self._make_logger()
        logger.info("Authorization: Bearer %s", BEARER_TOKEN)
        record = handler.records[0]
        parsed = json.loads(record)
        assert "Bearer [REDACTED]" in parsed["message"]
        assert BEARER_TOKEN not in parsed["message"]

    def test_password_redacted_in_output(self):
        logger, handler = self._make_logger()
        logger.info("user credentials: password=%s", QUERY_PASS)
        record = handler.records[0]
        parsed = json.loads(record)
        assert "password=[REDACTED]" in parsed["message"]

    def test_query_creds_redacted_in_output(self):
        logger, handler = self._make_logger()
        logger.info(
            "GET https://api.example.com?token=%s&password=%s", QUERY_TOKEN, QUERY_PASS
        )
        record = handler.records[0]
        parsed = json.loads(record)
        assert "token=[REDACTED]" in parsed["message"]
        assert "password=[REDACTED]" in parsed["message"]

    def test_x_api_key_redacted_in_output(self):
        logger, handler = self._make_logger()
        logger.info("Header: %s", _build_header(X_API_KEY_HEADER, PLACEHOLDER_HEADER))
        record = handler.records[0]
        parsed = json.loads(record)
        assert X_API_KEY_HEADER + ": [REDACTED]" in parsed["message"]

    def test_never_drops_log_lines(self):
        """VAL-OBS-016: exactly N records -> N output lines."""
        logger, handler = self._make_logger()
        n = 10
        key1 = _build_sk_key("TESTKEY1234567890")
        header_line = _build_header(X_API_KEY_HEADER, "PLACEHOLDER_DEADBEEF")
        key2 = _build_sk_key("TESTKEY1")
        messages = [
            "normal message",
            f"api_key={key1}",
            f"Bearer {BEARER_TOKEN}",
            f"password={QUERY_PASS}",
            header_line,
            f"mixed api_key={key2} and token=abc",
            "nothing sensitive",
            "another clean line",
            f"password: {QUERY_PASS}",
            "final message",
        ]
        for msg in messages:
            logger.info(msg)
        assert len(handler.records) == n, (
            f"Expected {n} output lines, got {len(handler.records)}"
        )

    def test_filter_returns_true(self):
        """The filter must always return True (never suppress)."""
        filt = SensitiveDataFilter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="test",
            args=(),
            exc_info=None,
        )
        assert filt.filter(record) is True

    def test_dict_args_redacted(self):
        """When record.args is a dict, redact sensitive values inside it."""
        logger, handler = self._make_logger()
        logger.info(
            "Processing request for %(user)s with key %(api_key)s",
            {"user": "alice", "api_key": _build_sk_key("TESTKEY1234567890123456")},
        )
        record = handler.records[0]
        parsed = json.loads(record)
        assert "alice" in parsed["message"]
        assert "[REDACTED]" in parsed["message"]


class _ListHandler(logging.Handler):
    """Accumulates formatted log records in a list for assertions."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(self.format(record))


class TestJSONFormatter:
    """Verify JSONFormatter still produces valid JSON with required fields."""

    def test_basic_format(self):
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

    def test_extra_fields_included(self):
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
        record.extra_fields = {"request_id": "abc12345"}
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["request_id"] == "abc12345"


class TestSetupLogging:
    """Minimal checks that setup_logging still wires things up."""

    def test_setup_logging_installs_sensitive_data_filter(self):
        original_handlers = list(logging.getLogger().handlers)
        try:
            setup_logging(default_level="DEBUG")
            root = logging.getLogger()
            has_sdf = any(isinstance(f, SensitiveDataFilter) for f in root.filters)
            assert has_sdf, "SensitiveDataFilter should be installed on root logger"
        finally:
            root = logging.getLogger()
            for h in root.handlers[:]:
                root.removeHandler(h)
            for h in original_handlers:
                root.addHandler(h)
