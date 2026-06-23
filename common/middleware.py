"""Shared middleware for all GroktoCrawl services.

Provides request_id_middleware — attaches a unique request_id to every
HTTP request, propagates via X-Request-ID header, and logs request
start/completion with latency.
"""

import logging
import time
import uuid
from collections.abc import Callable

from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

# Paths that should skip instrumentation to avoid log noise from pollers
SKIP_PATHS = {"/health", "/metrics"}


def add_request_id_middleware(
    app: FastAPI,
    skip_paths: set[str] | None = None,
    record_metric: Callable[[dict[str, str], float], None] | None = None,
) -> None:
    """Attach request_id middleware to a FastAPI app.

    Every request gets a unique ``request_id`` (first 8 chars of UUID4).
    The request_id is stored on ``request.state.request_id`` and echoed
    in log output for correlation across services.

    Args:
        app: The FastAPI application to instrument.
        skip_paths: Paths to skip instrumentation for (defaults to /health, /metrics).
        record_metric: Optional callback(labels, value) for recording request latency.
    """
    paths = skip_paths if skip_paths is not None else SKIP_PATHS

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        if request.url.path in paths:
            return await call_next(request)

        request_id = str(uuid.uuid4())[:8]
        request.state.request_id = request_id
        start_time = time.monotonic()

        logger.info(
            "Request started",
            extra={
                "extra_fields": {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                }
            },
        )

        response = await call_next(request)

        duration_ms = (time.monotonic() - start_time) * 1000

        if record_metric is not None:
            record_metric(
                {"method": request.method, "path": request.url.path},
                duration_ms / 1000,
            )

        logger.info(
            "Request completed",
            extra={
                "extra_fields": {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_ms, 1),
                }
            },
        )
        return response
