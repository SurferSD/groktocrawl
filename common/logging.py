"""Shared structured logging for all GroktoCrawl services.

Provides JSONFormatter, setup_logging(), and SensitiveDataFilter — import
in each service's app factory to get consistent JSON-line logging with
configurable log levels and automatic sensitive-data scrubbing.
"""

import json
import logging
import os
import re

# ── Sensitive data patterns ──────────────────────────────────────
# Each pattern must preserve non-sensitive context and only replace
# the sensitive value portion with [REDACTED].

_SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    # Order matters — broader key=value patterns first to avoid partial
    # redaction (e.g. ``api_key=sk-foo`` should become ``api_key=[REDACTED]``
    # rather than ``api_key=sk-[REDACTED]``).
    # 1a. Generic api_key=/api-key= (case-insensitive, any context)
    (r"\b(api[_-]key\s*=\s*)\S+", r"\1[REDACTED]"),
    # 1b. password= / password: (case-insensitive, any context)
    (r"\b(password\s*[=:]\s*)\S+", r"\1[REDACTED]"),
    # 2. Query-string credentials (?password=, ?token=, ?api_key=, ?api-key=, ?secret=)
    (r"([?&](?:password|token|api[_-]key|secret)=)[^&\s]+", r"\1[REDACTED]"),
    # 3. X-API-Key header value
    (r"\b(X-API-Key:\s*)\S+", r"\1[REDACTED]"),
    # 4. Bearer tokens (JWT or opaque, >= 6 chars)
    (r"\b(Bearer\s+)[A-Za-z0-9._-]{6,}\b", r"\1[REDACTED]"),
    # 5. Standalone API key prefixes (sk-/pk-, >= 6 chars)
    (r"\b(sk-[A-Za-z0-9_-]{6,})\b", r"sk-[REDACTED]"),
    (r"\b(pk-[A-Za-z0-9_-]{6,})\b", r"pk-[REDACTED]"),
]


def _redact_message(msg: str) -> str:
    """Apply all sensitive-data patterns to *msg* and return the redacted result."""
    for pattern, replacement in _SENSITIVE_PATTERNS:
        msg = re.sub(pattern, replacement, msg, flags=re.IGNORECASE)
    return msg


class SensitiveDataFilter(logging.Filter):
    """Logging filter that redacts sensitive data from log records.

    Scrubs API keys, bearer tokens, passwords, query-string credentials,
    and auth headers from ``record.msg`` and ``record.args`` dictionaries.

    The filter *never* drops log lines — it always returns ``True``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Interpolate the message early so we can redact the final text,
        # then replace msg so that subsequent getMessage() returns the
        # already-redacted value.
        formatted = record.getMessage()
        redacted = _redact_message(formatted)
        if redacted != formatted:
            record.msg = redacted
            record.args = ()  # type: ignore[assignment]
        elif record.args:
            # If args is a dict, redact the dict values in place.
            if isinstance(record.args, dict):
                for k, v in list(record.args.items()):
                    if isinstance(v, str) and _redact_message(v) != v:
                        record.args[k] = _redact_message(v)  # type: ignore[index]
        return True


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter.

    Produces one JSON object per line with fields:
    timestamp, level, logger, message, and optional extra fields.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in getattr(record, "extra_fields", {}).items():
            log_entry[key] = value
        if record.exc_info and record.exc_info[0]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


class ErrorCountingHandler(logging.Handler):
    """Logging handler that increments a Prometheus counter on ERROR-level records.

    The counter ``log_errors_total`` carries a ``service`` label so that
    per-service error rates can be queried in Prometheus.

    Args:
        service_name: Value for the ``service`` label.
    """

    def __init__(self, service_name: str = "unknown") -> None:
        super().__init__(level=logging.ERROR)
        self.service_name = service_name
        self._counter = None

    def _get_counter(self):  # type: ignore[no-untyped-def]
        """Lazy-import METRICS to avoid circular import on module load."""
        if self._counter is None:
            from common.metrics import METRICS  # fmt: skip

            self._counter = METRICS.counter(
                "log_errors_total",
                "Total error-level log messages",
                ["service"],
            )
        return self._counter

    def emit(self, record: logging.LogRecord) -> None:
        self._get_counter().inc({"service": self.service_name})


def setup_logging(
    default_level: str | None = None,
    quiet_third_party: bool = True,
    service_name: str | None = None,
) -> None:
    """Configure structured JSON logging with sensitive-data scrubbing.

    Replaces the default log format with JSON lines. Sets the root logger
    to the level from ``LOG_LEVEL`` env var (defaults to "INFO").

    A :class:`SensitiveDataFilter` is added to the root logger so that
    API keys, bearer tokens, passwords, and similar secrets are
    automatically redacted from all log output.

    When *service_name* is provided, an :class:`ErrorCountingHandler` is
    also installed that increments ``log_errors_total{service=...}`` on
    every ERROR-level record.

    Args:
        default_level: Override the env-configured log level.
        quiet_third_party: If True, suppress noisy logs from httpx/httpcore/urllib3.
        service_name: Service name for the ``log_errors_total`` counter label.
    """
    log_level = (default_level or os.getenv("LOG_LEVEL", "INFO")).upper()  # type: ignore[union-attr]
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    root = logging.getLogger()
    root.setLevel(log_level)
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.addHandler(handler)

    # Install sensitive-data scrubbing on the root logger.
    # The filter runs before the formatter and interpolates/redacts
    # so that getMessage() inside the formatter returns clean text.
    _install_sensitive_data_filter(root)

    # Install error-counting handler when a service name is provided.
    if service_name:
        error_handler = ErrorCountingHandler(service_name=service_name)
        root.addHandler(error_handler)

    if quiet_third_party:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)


def _install_sensitive_data_filter(logger: logging.Logger) -> None:
    """Add a :class:`SensitiveDataFilter` if one isn't already installed."""
    for f in logger.filters:
        if isinstance(f, SensitiveDataFilter):
            return
    logger.addFilter(SensitiveDataFilter())
