"""FastAPI application for the scraper service.

Single endpoint: POST /scrape — takes a URL, returns clean markdown.
"""

import logging
from contextlib import asynccontextmanager
from enum import Enum

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .cookie_store import close_client, get_client
from .exceptions import GroktoCrawlError, UpstreamError
from .fetch import smart_scrape
from .meta import fetch_meta_tags

logger = logging.getLogger(__name__)


class VerbosityLevel(str, Enum):
    compact = "compact"       # ~300 chars of body text
    standard = "standard"     # Current behavior (readability extraction)
    full = "full"             # Complete page text including structural markup


class SectionCategory(str, Enum):
    header = "header"
    navigation = "navigation"
    banner = "banner"
    body = "body"
    sidebar = "sidebar"
    footer = "footer"
    metadata = "metadata"


class ExtrasOptions(BaseModel):
    links: int | None = None        # Max external links to extract
    imageLinks: int | None = None   # Max image URLs to extract
    codeBlocks: int | None = None   # Max code blocks to extract


class ContentsOptions(BaseModel):
    text: bool | dict | None = None          # True = full text, or dict with verbosity/sections
    highlights: bool | dict | None = None    # True = auto highlights, or dict with query/maxCharacters
    summary: bool | dict | None = None       # True = auto summary, or dict with query/maxTokens
    extras: ExtrasOptions | None = None


@asynccontextmanager
async def lifespan(app):
    """Connect to Valkey on startup, close on shutdown.

    Also loads site adapters (YouTube, etc.) — safe to call even
    if the adapters package is not yet installed.
    """
    from .adapters.base import get_registry

    try:
        registry = get_registry()
        registry.load_all()
        logger.info("Loaded %d site adapters", len(registry._entries))
    except Exception as exc:
        logger.warning("Failed to load adapters: %s", exc)

    await get_client()
    yield
    await close_client()


app = FastAPI(title="GroktoCrawl Scraper", version="0.1.0", lifespan=lifespan)


class ScrapeRequest(BaseModel):
    url: str
    contents: ContentsOptions | None = None  # Optional content extraction options


class DownloadData(BaseModel):
    """Binary content metadata for non-HTML responses."""

    filename: str
    content_type: str
    size: int
    data_url: str | None = None


class ScrapeResponse(BaseModel):
    success: bool
    data: dict | None = None
    error: str | None = None


class MetaResponse(BaseModel):
    success: bool = True
    title: str | None = None
    description: str | None = None
    og_description: str | None = None
    url: str | None = None
    error: str | None = None


class ErrorResponse(BaseModel):
    """Standard error response body for the scraper service."""

    success: bool = False
    error: str = "An unexpected error occurred"
    error_code: str = "INTERNAL_ERROR"
    details: list | dict | None = None


@app.exception_handler(GroktoCrawlError)
async def groktocrawl_error_handler(request: Request, exc: GroktoCrawlError):
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=exc.detail,
            error_code=exc.error_code,
            details=exc.details,
        ).model_dump(),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
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
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    details_list = []
    for err in exc.errors():
        loc = err.get("loc", [])
        field = ".".join(str(p) for p in loc)
        details_list.append({"field": field, "message": err.get("msg", "")})
    return JSONResponse(
        status_code=422,
        content=ErrorResponse(
            error="Validation failed",
            error_code="INVALID_REQUEST",
            details=details_list,
        ).model_dump(),
    )


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/scrape", response_model=ScrapeResponse)
async def scrape(request: ScrapeRequest):
    """Scrape a URL and return its content as clean markdown."""
    try:
        result = await smart_scrape(request.url)
        if result.get("error"):
            raise UpstreamError(detail=result["error"])
        markdown = result.get("markdown", "")
        raw_html = result.get("raw_html_start", "")

        # ── Content extraction options (ADR pending) ─────────────
        extras_data: dict | None = None
        if request.contents:
            # Section filtering and verbosity
            if request.contents.text:
                from .extract import filter_sections

                text_opts: dict = (
                    request.contents.text
                    if isinstance(request.contents.text, dict)
                    else {}
                )
                verbosity = text_opts.get("verbosity", "standard")
                include_sections = text_opts.get("include")
                exclude_sections = text_opts.get("exclude")

                if raw_html and verbosity != "standard":
                    markdown = filter_sections(
                        raw_html,
                        include=include_sections,
                        exclude=exclude_sections,
                        verbosity=verbosity,
                    )
                elif verbosity == "compact" and markdown:
                    # Fallback: compact verbosity from markdown (no HTML needed)
                    markdown = markdown[:300]
                elif verbosity == "full" and raw_html:
                    from .fetch import html_to_markdown

                    # Full verbosity: bypass readability, render entire page
                    from markdownify import markdownify as md

                    soup: None = None
                    try:
                        from bs4 import BeautifulSoup as _BS

                        soup = _BS(raw_html, "html.parser")
                    except Exception:
                        pass
                    if soup:
                        markdown = md(str(soup), heading_style="ATX", strip=[])
                    else:
                        markdown = html_to_markdown(raw_html)
                elif include_sections or exclude_sections:
                    if raw_html:
                        markdown = filter_sections(
                            raw_html,
                            include=include_sections,
                            exclude=exclude_sections,
                            verbosity=verbosity,
                        )

            # Extras extraction
            if request.contents.extras:
                from .extract import extract_extras

                if raw_html:
                    extras_data = extract_extras(raw_html, request.contents.extras)

        data = {
            "markdown": markdown,
            "source": result.get("source", "unknown"),
            "url": request.url,
            "quality": result.get("quality"),
            "politeness": result.get("politeness"),
            "metadata": result.get("metadata"),
        }
        if extras_data:
            data["extras"] = extras_data
        if result.get("download"):
            data["download"] = result["download"]
        return ScrapeResponse(success=True, data=data)
    except Exception as e:
        logger.exception("Scrape failed for %s", request.url)
        raise UpstreamError(detail=str(e)) from e


@app.post("/scrape/meta", response_model=MetaResponse)
async def scrape_meta(request: ScrapeRequest):
    """Extract meta tags from a URL using raw HTML (cheap, one GET).

    Returns <title>, <meta name="description">, and
    <meta property="og:description"> without full page rendering.
    """
    try:
        result = await fetch_meta_tags(request.url)
        return MetaResponse(
            success=True,
            title=result.get("title"),
            description=result.get("description"),
            og_description=result.get("og_description"),
            url=request.url,
        )
    except Exception as e:
        logger.exception("Meta fetch failed for %s", request.url)
        raise UpstreamError(detail=str(e)) from e
