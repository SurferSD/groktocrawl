"""GroktoCrawl web portal — single-search-bar UI for human users."""

import json
import logging
import os

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from common.circuit_breaker import CircuitBreaker, CircuitOpenError
from common.logging import setup_logging
from common.metrics import METRICS
from common.middleware import add_request_id_middleware

setup_logging()
logger = logging.getLogger(__name__)

AGENT_BASE_URL = os.getenv("AGENT_BASE_URL", "http://agent-svc:8080")
# Strip any trailing slash so the join with "/v2/answer" is always clean.
BASE = AGENT_BASE_URL.rstrip("/")
ANSWER_URL = f"{BASE}/v2/answer"

# ── Circuit breaker for agent-svc proxy ─────────────────────────
# Protects against cascading failures when agent-svc is unhealthy.
_agent_circuit_breaker = CircuitBreaker(
    failure_threshold=int(os.getenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")),
    cooldown_seconds=float(os.getenv("CIRCUIT_BREAKER_COOLDOWN_SECONDS", "30.0")),
)

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)

app = FastAPI(
    title="GroktoCrawl Portal",
    version="0.1.0",
    description="Web portal for GroktoCrawl — self-hosted AI research.",
)

# ── Instrumentation ──────────────────────────────────────────
add_request_id_middleware(app)
_queries_counter = METRICS.counter("portal_queries_total", "Total portal queries")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "portal-svc"}


@app.get("/metrics")
async def metrics():
    """Prometheus-compatible OpenMetrics endpoint."""
    return PlainTextResponse(
        METRICS.generate_openmetrics(),
        media_type="application/openmetrics-text; version=1.0.0",
    )


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.post("/ask")
async def ask(query: str = Form(...), num_sources: int = Form(5)):
    """Proxy a grounded Q&A query to agent-svc, streaming SSE results back.

    Errors from the downstream agent are communicated as SSE ``error``
    events so the frontend can display them inline.  Transport-level
    failures (e.g. agent unreachable) are also caught and converted to
    SSE error events.

    Circuit breaker integration: if agent-svc returns 5 consecutive 5xx
    errors, subsequent requests are fast-failed with a 503 ``circuit_open``
    SSE error without making an HTTP call.
    """
    _queries_counter.inc()

    async def proxy_stream():
        # Circuit breaker: fast-fail check before making any HTTP call
        try:
            await _agent_circuit_breaker.check()
        except CircuitOpenError as exc:
            logger.warning(
                "Circuit breaker open, fast-failing request to %s", ANSWER_URL
            )
            error_body = json.dumps(exc.detail)
            yield f"event: error\ndata: {error_body}\n\n".encode()
            return

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    ANSWER_URL,
                    json={
                        "query": query,
                        "num_sources": num_sources,
                        "stream": True,
                    },
                ) as response:
                    if response.status_code >= 400:
                        body = await response.aread()
                        if response.status_code >= 500:
                            await _agent_circuit_breaker.record_failure()
                        yield (
                            f"event: error\ndata: {body.decode(errors='replace')}\n\n"
                        ).encode()
                        return
                    await _agent_circuit_breaker.record_success()
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except httpx.ConnectError:
            await _agent_circuit_breaker.record_failure()
            logger.warning("Agent unreachable at %s", ANSWER_URL)
            yield b"event: error\ndata: Service unavailable\n\n"

    return StreamingResponse(proxy_stream(), media_type="text/event-stream")
