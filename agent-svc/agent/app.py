"""FastAPI application entrypoint for GroktoCrawl."""

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from redis import Redis

from common.features import is_enabled
from common.logging import setup_logging
from common.metrics import METRICS
from common.middleware import add_request_id_middleware

from .analytics_exporter import start_analytics_exporter
from .api import router
from .auth import (
    AUTH_ENABLED,
    SECURITY_WARNING_BODY,
    SECURITY_WARNING_HEADER,
    verify_api_key,
)
from .exceptions import GroktoCrawlError
from .health import check_all
from .llm import LLMClient
from .models import ErrorDetail, ErrorResponse
from .rate_limiter import SlidingWindowRateLimiter
from .scraper_client import ScraperClient
from .searxng_client import SearXNGClient
from .settings import load_settings
from .store import JobStore
from .tasks import TaskTracker

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = load_settings()
    setup_logging(default_level=settings.log_level, service_name="agent-svc")

    app = FastAPI(
        title="GroktoCrawl",
        version="0.6.0",
        description="Self-hosted, Firecrawl-compatible web scraping and AI research API. MIT licensed.",
        servers=[
            {"url": "http://localhost:8080", "description": "Local development"},
        ],
        contact={
            "name": "GroktoCrawl",
            "url": "https://github.com/groktopus/groktocrawl",
        },
        license_info={
            "name": "MIT",
            "url": "https://github.com/groktopus/groktocrawl/blob/main/LICENSE",
        },
    )

    redis_url = (
        f"redis://{settings.valkey_host}:{settings.valkey_port}/{settings.valkey_db}"
    )
    conn = Redis.from_url(redis_url, decode_responses=True)
    store = JobStore(redis_url)
    scraper_client = ScraperClient(settings.scraper_url)
    searxng_client = SearXNGClient(settings.searxng_url)
    llm_client = LLMClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )

    # ── Rate limiter ──────────────────────────────────────────────
    rate_limit_count, rate_limit_window = SlidingWindowRateLimiter.parse_limit(
        settings.search_rate_limit
    )
    rate_limiter = SlidingWindowRateLimiter(conn, rate_limit_count, rate_limit_window)

    # ── Metrics initialization ────────────────────────────────────
    METRICS.counter("search_calls_total", "Total search calls", ["status"])
    # Info metric for version identification (kept for backward compat)
    METRICS.gauge("groktocrawl_info", "GroktoCrawl version info").set(value=1.0)

    # ── Feature toggle observability ─────────────────────────────
    # Log effective state of every FEATURE_* toggle and register
    # groktocrawl_feature_enabled{feature=...} gauges.
    _feature_gauge = METRICS.gauge(
        "groktocrawl_feature_enabled",
        "Feature toggle enabled status (1=enabled, 0=disabled)",
        ["feature"],
    )
    for env_key, env_val in sorted(os.environ.items()):
        if env_key.startswith("FEATURE_"):
            feature_name = env_key[len("FEATURE_") :].lower()
            enabled = is_enabled(feature_name)
            logger.info(
                "Feature toggle %s enabled=%s (from %s=%s)",
                feature_name,
                str(enabled),
                env_key,
                env_val,
            )
            _feature_gauge.set(
                {"feature": feature_name},
                1.0 if enabled else 0.0,
            )

    # ── App state ───────────────────────────────────────────────
    app.state.redis = conn
    app.state.job_store = store
    app.state.scraper_client = scraper_client
    app.state.searxng_client = searxng_client
    app.state.llm_client = llm_client
    app.state.valkey_url = redis_url
    app.state.scraper_url = settings.scraper_url
    app.state.searxng_url = settings.searxng_url
    app.state.llm_base_url = settings.llm_base_url
    app.state.llm_api_key = settings.llm_api_key
    app.state.llm_model = settings.llm_model
    app.state.semantic_url = settings.semantic_url
    app.state.rate_limiter = rate_limiter
    app.state.max_searches_per_request = settings.max_searches_per_request
    app.state.task_tracker = TaskTracker()

    # ── Middleware: request_id ───────────────────────────────────
    def _record_metric(labels: dict[str, str], value: float) -> None:
        METRICS.histogram(
            "http_request_duration_seconds",
            "HTTP request latency by path and method",
            ["method", "path"],
        ).observe(labels, value)

    add_request_id_middleware(app, record_metric=_record_metric)

    # ── Security warning middleware ──────────────────────────────
    @app.middleware("http")
    async def security_warning_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if not AUTH_ENABLED:
            response.headers[SECURITY_WARNING_HEADER] = (
                "No API key configured. API is publicly accessible. "
                "Set API_KEY=your-key in .env to enable authentication. "
                "See https://github.com/groktopus/groktocrawl#security"
            )
        return response

    # ── Health endpoint (always unauthenticated) ─────────────────
    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Return aggregate health status with per-dependency probes.

        Response shape (backward-compatible):
            {"status": "ok", "checks": {"valkey": {...}, "searxng": {...}, ...}}

        The top-level ``status`` field matches the existing contract for
        simple liveness checks. The ``checks`` field contains per-dependency
        probe results with status, latency_ms, and detail.
        """
        result = await check_all(
            valkey_url=app.state.valkey_url,
            searxng_url=app.state.searxng_url,
            scraper_url=app.state.scraper_url,
            browser_url="http://browser-svc:8012",
            portal_url="http://portal-svc:8081",
        )
        # Record health check outcomes as metrics
        dh_gauge = METRICS.gauge(
            "dependency_health",
            "Dependency health status (1=ok, 0=down/-1=degraded)",
            ["dependency"],
        )
        for name, probe in result.get("checks", {}).items():
            status_val = 0.0
            if probe.get("status") == "ok":
                status_val = 1.0
            elif probe.get("status") == "degraded":
                status_val = -1.0
            dh_gauge.set({"dependency": name}, status_val)

        if not AUTH_ENABLED:
            result["security"] = {
                "auth_enabled": False,
                "warning": SECURITY_WARNING_BODY,
                "docs": "https://github.com/groktopus/groktocrawl#security",
            }

        return result

    # ── Metrics endpoint (always unauthenticated) ────────────────
    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        """OpenMetrics-format metrics endpoint for Prometheus scraping.

        Returns counters, histograms, and gauges collected during agent-svc
        operation. See ``metrics.py`` for the full metric set.
        """
        # Update queue depth gauge before exporting
        try:
            active_jobs = app.state.job_store.list_active_jobs(
                status="processing", limit=1000
            )
            METRICS.gauge("queue_depth", "Current number of processing jobs").set(
                value=float(len(active_jobs))
            )
        except Exception:
            METRICS.gauge("queue_depth", "Current number of processing jobs").set(
                value=-1.0
            )

        return PlainTextResponse(
            content=METRICS.generate_openmetrics(),
            media_type="application/openmetrics-text; version=1.0.0",
        )

    # ── Exception handlers ──────────────────────────────────────
    @app.exception_handler(GroktoCrawlError)
    async def groktocrawl_error_handler(
        request: Request, exc: GroktoCrawlError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=exc.detail,
                error_code=exc.error_code,
                details=exc.details,
            ).model_dump(),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        status_code = exc.status_code
        error_code_map = {
            400: "INVALID_REQUEST",
            401: "AUTH_ERROR",
            403: "AUTH_ERROR",
            404: "NOT_FOUND",
            422: "INVALID_REQUEST",
            429: "RATE_LIMITED",
            502: "UPSTREAM_ERROR",
        }
        error_code = error_code_map.get(status_code, "INTERNAL_ERROR")
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return JSONResponse(
            status_code=status_code,
            content=ErrorResponse(error=detail, error_code=error_code).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details_list = []
        for err in exc.errors():
            loc = err.get("loc", [])
            field = ".".join(str(p) for p in loc)
            details_list.append(ErrorDetail(field=field, message=err.get("msg", "")))
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="Validation failed",
                error_code="INVALID_REQUEST",
                details=details_list,
            ).model_dump(),
        )

    # ── Include API router with auth dependency ─────────────────
    app.include_router(router, dependencies=[Depends(verify_api_key)])

    @app.on_event("startup")
    async def startup_event() -> None:
        """Start the analytics counter exporter on server startup.

        This runs inside a running event loop (unlike module-level
        ``asyncio.create_task()`` which would fail at import time).
        """
        app.state.task_tracker.create_background_task(
            start_analytics_exporter(redis_url=app.state.valkey_url)
        )

    @app.on_event("shutdown")
    async def shutdown_event() -> None:
        await app.state.task_tracker.shutdown(grace_period=5.0)
        await app.state.scraper_client.close()
        await app.state.searxng_client.close()
        await app.state.llm_client.close()

    return app


app = create_app()
