"""Deterministic search fixture service for local integration tests.

Supports both GET (SearXNG JSON API compatible) and POST.
"""

import logging

from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from common.logging import setup_logging
from common.metrics import METRICS
from common.middleware import add_request_id_middleware

logger = logging.getLogger(__name__)


class SearchRequest(BaseModel):
    q: str
    limit: int = 5


DATASET = [
    {
        "url": "http://test-site:8000/llms.txt",
        "title": "Fixture llms.txt",
        "description": "Site publishes llms.txt.",
        "keywords": ["llms", "pricing", "agent"],
    },
    {
        "url": "http://test-site:8000/pricing",
        "title": "Fixture Pricing",
        "description": "Markdown negotiation page.",
        "keywords": ["pricing", "markdown", "accept"],
    },
    {
        "url": "http://test-site:8000/dynamic",
        "title": "Fixture Dynamic",
        "description": "JS-rendered page.",
        "keywords": ["dynamic", "browser", "js"],
    },
    {
        "url": "http://test-site:8000/",
        "title": "Fixture Home",
        "description": "Crawlable homepage.",
        "keywords": ["crawl", "map", "site"],
    },
]


def _search(q: str, limit: int = 5):
    q = q.lower()
    ranked = []
    for item in DATASET:
        score = sum(1 for kw in item["keywords"] if kw in q)
        ranked.append((score, item))
    ranked.sort(key=lambda x: x[0], reverse=True)
    results: list[dict[str, str]] = []
    for score, item in ranked:
        if score > 0 or not results:
            results.append(
                {
                    "url": str(item["url"]),
                    "title": str(item["title"]),
                    "description": str(item["description"]),
                }
            )
        if len(results) >= limit:
            break
    return {"results": results}


def create_app() -> FastAPI:
    setup_logging()

    app = FastAPI(title="GroktoCrawl Search Fixture", version="0.1.0")

    # Register a basic metric so /metrics output has content
    METRICS.counter("search_requests_total", "Total search requests", ["status"])

    # Request-ID tracing middleware (skips /health and /metrics)
    def _record_metric(labels: dict[str, str], value: float) -> None:
        METRICS.histogram(
            "http_request_duration_seconds",
            "HTTP request duration in seconds",
            ["method", "path"],
        ).observe(labels, value)

    add_request_id_middleware(app, record_metric=_record_metric)

    logger.info(
        "search-svc starting up", extra={"extra_fields": {"service": "search-svc"}}
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics():
        return PlainTextResponse(
            content=METRICS.generate_openmetrics(),
            media_type="application/openmetrics-text; version=1.0.0",
        )

    @app.get("/search")
    async def search_get(q: str = Query(""), limit: int = Query(5)):
        return _search(q, limit)

    @app.post("/search")
    async def search_post(req: SearchRequest):
        return _search(req.q, req.limit)

    return app


app = create_app()
