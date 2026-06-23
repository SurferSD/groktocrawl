"""Worker entrypoint and processing functions for GroktoCrawl jobs."""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from .metrics import METRICS
from .research import run_extract, run_research
from .scraper_client import ScraperClient
from .settings import load_settings
from .store import JobStore
from .webhook import deliver_webhook

logger = logging.getLogger(__name__)


def _get_worker_settings() -> Any:
    return load_settings()


async def _run_job_with_observability(
    job_id: str,
    job_type: str,
    store: JobStore,
    webhook_config: dict[str, Any] | None,
    work_fn: Callable[[], Coroutine[Any, Any, Any]],
    cleanup_fn: Callable[[], Coroutine[Any, Any, None]] | None = None,
) -> None:
    """Execute work_fn with standard observability scaffolding.

    Encapsulates metrics recording, store completion/failure, webhook
    delivery, and cleanup — the identical scaffolding shared by all
    worker processing functions.
    """
    start = time.monotonic()
    METRICS.counter("jobs_submitted_total", "Total jobs submitted", ["type"]).inc(
        {"type": job_type}
    )
    try:
        result = await work_fn()
        store.complete_job(job_id, result)
        await deliver_webhook(webhook_config, "completed", job_id, result)
        elapsed = time.monotonic() - start
        METRICS.histogram(
            "job_duration_seconds", "Job processing duration", ["type", "status"]
        ).observe({"type": job_type, "status": "completed"}, elapsed)
        METRICS.counter("jobs_completed_total", "Total completed jobs", ["type"]).inc(
            {"type": job_type}
        )
        logger.info("%s job %s completed in %.2fs", job_type, job_id, elapsed)
    except Exception as e:
        logger.exception("%s job %s failed", job_type, job_id)
        store.fail_job(job_id, str(e))
        await deliver_webhook(webhook_config, "failed", job_id, {"error": str(e)})
        elapsed = time.monotonic() - start
        METRICS.histogram(
            "job_duration_seconds", "Job processing duration", ["type", "status"]
        ).observe({"type": job_type, "status": "failed"}, elapsed)
        METRICS.counter("jobs_failed_total", "Total failed jobs", ["type"]).inc(
            {"type": job_type}
        )
    finally:
        if cleanup_fn:
            await cleanup_fn()


async def _process_agent_async(
    job_id: str,
    prompt: str,
    urls: list[str] | None,
    schema_: dict[str, Any] | None,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    searxng_url: str,
    scraper_url: str,
    webhook_config: dict[str, Any] | None = None,
    requested_model: str | None = None,
) -> None:
    settings = _get_worker_settings()
    store = JobStore(
        f"redis://{settings.valkey_host}:{settings.valkey_port}/{settings.valkey_db}"
    )

    async def work_fn() -> dict[str, Any]:
        return await run_research(
            prompt=prompt,
            urls=urls,
            schema=schema_,
            searxng_url=searxng_url,
            scraper_url=scraper_url,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            requested_model=requested_model,
        )

    await _run_job_with_observability(job_id, "agent", store, webhook_config, work_fn)


async def _process_crawl_async(
    job_id: str,
    url: str,
    max_pages: int,
    max_depth: int,
    scraper_url: str,
    webhook_config: dict[str, Any] | None = None,
    task_tracker: Any = None,
    ignore_query_parameters: bool = False,
    include_paths: list[str] | None = None,
    exclude_paths: list[str] | None = None,
    regex_on_full_url: bool = False,
    verbose: bool = False,
    sitemap_mode: str = "include",
    crawl_entire_domain: bool = False,
    allow_subdomains: bool = False,
    allow_external_links: bool = False,
    max_concurrency: int = 3,
    delay: float | None = None,
    ignore_robots_txt: bool = False,
    robots_user_agent: str | None = None,
    scrape_options: dict | None = None,
) -> None:
    """Process a crawl job with full lifecycle support.

    Lifecycle webhooks (when configured):
        - ``crawl.started``: fired before the crawl begins
        - ``crawl.page``: fired after each individual page is scraped
        - ``crawl.completed``: fired on terminal states (completed, cancelled)
        - ``crawl.failed``: fired on unexpected exception

    Cancellation is cooperative: DELETE /v2/crawl/{id} sets the job meta
    status to ``cancelled`` in Redis; the engine checks this between
    page scrapes and stops early. The job store is NOT overwritten when
    the engine detects cancellation (``cancel_job()`` already set it).
    """
    settings = _get_worker_settings()
    store = JobStore(
        f"redis://{settings.valkey_host}:{settings.valkey_port}/{settings.valkey_db}"
    )
    scraper = ScraperClient(scraper_url)
    start = time.monotonic()
    job_type = "crawl"

    METRICS.counter("jobs_submitted_total", "Total jobs submitted", ["type"]).inc(
        {"type": job_type}
    )

    try:
        # ── Fire crawl.started webhook ────────────────────────
        # Per VAL-PARITY-030: crawl.started fires BEFORE any page scraping.
        # The data field is an empty list (no pages yet), and metadata is
        # echoed from the webhook config (VAL-PARITY-009).
        if task_tracker is not None:
            task_tracker.create_background_task(
                deliver_webhook(
                    webhook_config,
                    "crawl.started",
                    job_id,
                    data=[],
                    task_tracker=task_tracker,
                )
            )
        else:
            await deliver_webhook(
                webhook_config,
                "crawl.started",
                job_id,
                data=[],
            )

        from .crawl_cache import CrawlCache
        from .crawler import CrawlEngine, CrawlOptions

        crawl_cache = CrawlCache(
            f"redis://{settings.valkey_host}:{settings.valkey_port}/{settings.valkey_db}"
        )

        options = CrawlOptions(
            max_pages=max_pages,
            max_depth=max_depth,
            ignore_query_parameters=ignore_query_parameters,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
            regex_on_full_url=regex_on_full_url,
            verbose=verbose,
            sitemap_mode=sitemap_mode,
            allow_subdomains=allow_subdomains,
            allow_external_links=allow_external_links,
            crawl_entire_domain=crawl_entire_domain,
            max_concurrency=max_concurrency,
            delay=delay,
            ignore_robots_txt=ignore_robots_txt,
            robots_user_agent=robots_user_agent,
            max_duration_seconds=settings.crawl_max_duration_seconds,
            idle_timeout_seconds=settings.crawl_idle_timeout_seconds,
            scrape_options=scrape_options,
        )
        engine = CrawlEngine(
            scraper, store=store, options=options, crawl_cache=crawl_cache
        )

        # Per-page webhook callback using task_tracker (VAL-CONC-049)
        async def _page_callback(_job_id: str, page: dict[str, Any]) -> None:
            # Deliver webhook as a tracked background task to avoid
            # blocking the crawl loop on webhook delivery latency.
            # Per VAL-PARITY-006: data is an array containing one page document.
            if task_tracker is not None:
                task_tracker.create_background_task(
                    deliver_webhook(
                        webhook_config,
                        "crawl.page",
                        _job_id,
                        data=[page],
                        task_tracker=task_tracker,
                    )
                )
            else:
                await deliver_webhook(
                    webhook_config,
                    "crawl.page",
                    _job_id,
                    data=[page],
                )

        result = await engine.run(url, job_id=job_id, page_callback=_page_callback)

        # Fire-and-forget indexing for each page using task_tracker (VAL-CONC-049)
        for page in result.pages:
            page_url = page.get("url", "")
            markdown = page.get("markdown", "")
            idx_task = _index_page_async(page_url, "", markdown[:2000])
            if task_tracker is not None:
                task_tracker.create_background_task(idx_task)
            else:
                asyncio.create_task(idx_task)

        payload: dict[str, Any] = {
            "completed": result.completed,
            "total": result.total,
            "pages": result.pages,
            "errors": result.errors,
            "robots_blocked": result.robots_blocked,
        }
        if verbose:
            payload["filtered_out"] = result.filtered_out

        # ── Check if job was cancelled via DELETE ─────────────
        job_meta = store.get_job(job_id)
        was_cancelled = job_meta is not None and job_meta.get("status") == "cancelled"

        if was_cancelled:
            # Store is already marked cancelled by cancel_job();
            # do NOT overwrite with complete_job().
            logger.info("Crawl %s was cancelled — preserving cancelled status", job_id)
            if task_tracker is not None:
                task_tracker.create_background_task(
                    deliver_webhook(
                        webhook_config,
                        "crawl.completed",
                        job_id,
                        data=[],
                        task_tracker=task_tracker,
                    )
                )
            else:
                await deliver_webhook(
                    webhook_config,
                    "crawl.completed",
                    job_id,
                    data=[],
                )
        else:
            store.complete_job(job_id, payload)
            if task_tracker is not None:
                task_tracker.create_background_task(
                    deliver_webhook(
                        webhook_config,
                        "crawl.completed",
                        job_id,
                        data=[],
                        task_tracker=task_tracker,
                    )
                )
            else:
                await deliver_webhook(
                    webhook_config,
                    "crawl.completed",
                    job_id,
                    data=[],
                )

        elapsed = time.monotonic() - start

        # ── Existing job-type-agnostic metrics (keep for backward compat) ──
        METRICS.histogram(
            "job_duration_seconds", "Job processing duration", ["type", "status"]
        ).observe({"type": job_type, "status": "completed"}, elapsed)
        METRICS.counter("jobs_completed_total", "Total completed jobs", ["type"]).inc(
            {"type": job_type}
        )

        # ── Crawl-specific metrics ──────────────────────────────────────────
        crawl_status = "cancelled" if was_cancelled else "completed"
        METRICS.counter(
            "groktocrawl_crawl_jobs_total", "Total crawl jobs by status", ["status"]
        ).inc({"status": crawl_status})
        METRICS.histogram(
            "groktocrawl_crawl_duration_seconds",
            "Crawl job duration in seconds",
            ["status"],
        ).observe({"status": crawl_status}, elapsed)
        METRICS.counter(
            "groktocrawl_crawl_pages_scraped_total",
            "Total pages scraped by crawl jobs",
        ).inc(value=float(result.completed))

        logger.info("Crawl job %s completed in %.2fs", job_id, elapsed)

    except Exception as e:
        logger.exception("Crawl job %s failed", job_id)
        store.fail_job(job_id, str(e))
        if task_tracker is not None:
            task_tracker.create_background_task(
                deliver_webhook(
                    webhook_config,
                    "crawl.failed",
                    job_id,
                    data=[],
                    success=False,
                    error=str(e),
                    task_tracker=task_tracker,
                )
            )
        else:
            await deliver_webhook(
                webhook_config,
                "crawl.failed",
                job_id,
                data=[],
                success=False,
                error=str(e),
            )
        elapsed = time.monotonic() - start

        # ── Existing job-type-agnostic metrics ─────────────────────────────
        METRICS.histogram(
            "job_duration_seconds", "Job processing duration", ["type", "status"]
        ).observe({"type": job_type, "status": "failed"}, elapsed)
        METRICS.counter("jobs_failed_total", "Total failed jobs", ["type"]).inc(
            {"type": job_type}
        )

        # ── Crawl-specific metrics ─────────────────────────────────────────
        METRICS.counter(
            "groktocrawl_crawl_jobs_total", "Total crawl jobs by status", ["status"]
        ).inc({"status": "failed"})
        METRICS.histogram(
            "groktocrawl_crawl_duration_seconds",
            "Crawl job duration in seconds",
            ["status"],
        ).observe({"status": "failed"}, elapsed)
    finally:
        await scraper.close()


async def _process_batch_scrape_async(
    job_id: str,
    urls: list[str],
    scraper_url: str,
    webhook_config: dict[str, Any] | None = None,
    task_tracker: Any = None,
) -> None:
    settings = _get_worker_settings()
    store = JobStore(
        f"redis://{settings.valkey_host}:{settings.valkey_port}/{settings.valkey_db}"
    )
    scraper = ScraperClient(scraper_url)

    async def work_fn() -> dict[str, Any]:
        pages = []
        _index_batch = []
        for url in urls:
            result = await scraper.scrape(url)
            if result.get("success"):
                data = result["data"]
                pages.append({"url": url, "markdown": data.get("markdown", "")})
                metadata = data.get("metadata") or {}
                og = metadata.get("og") or {}
                meta = metadata.get("meta") or {}
                title = og.get("title") or meta.get("title") or data.get("title", "")
                # Accumulate for batch indexing (ADR-0030) instead of per-page
                _index_batch.append(
                    {
                        "url": url,
                        "title": title,
                        "content": data.get("markdown", "")[:2000],
                    }
                )
        if _index_batch:
            if task_tracker is not None:
                task_tracker.create_background_task(_index_batch_async(_index_batch))
            else:
                asyncio.create_task(_index_batch_async(_index_batch))
        payload = {"completed": len(pages), "total": len(urls), "pages": pages}
        return payload

    await _run_job_with_observability(
        job_id, "batch_scrape", store, webhook_config, work_fn, scraper.close
    )


async def _process_extract_async(
    job_id: str,
    urls: list[str],
    prompt: str | None,
    schema_: dict[str, Any] | None,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    scraper_url: str,
    webhook_config: dict[str, Any] | None = None,
) -> None:
    settings = _get_worker_settings()
    store = JobStore(
        f"redis://{settings.valkey_host}:{settings.valkey_port}/{settings.valkey_db}"
    )

    async def work_fn() -> dict[str, Any]:
        return await run_extract(
            urls=urls,
            prompt=prompt,
            schema=schema_,
            scraper_url=scraper_url,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
        )

    await _run_job_with_observability(job_id, "extract", store, webhook_config, work_fn)


async def _process_llmstxt_async(
    job_id: str,
    url: str,
    max_pages: int,
    scraper_url: str,
    webhook_config: dict[str, Any] | None = None,
) -> None:
    settings = _get_worker_settings()
    store = JobStore(
        f"redis://{settings.valkey_host}:{settings.valkey_port}/{settings.valkey_db}"
    )

    async def work_fn() -> dict[str, Any]:
        from .llmstxt import generate_llmstxt

        return await generate_llmstxt(url, max_pages, scraper_url)

    await _run_job_with_observability(job_id, "llmstxt", store, webhook_config, work_fn)


async def _index_page_async(url: str, title: str, content: str) -> None:
    """Fire-and-forget index a page in the vector index.

    Failure is logged but never propagated — indexing is best-effort.
    """
    try:
        from .semantic_client import SemanticClient

        settings = load_settings()
        semantic_url = settings.semantic_url
        client = SemanticClient(semantic_url)
        await client.index_page(url, title, content)
        await client.close()
        logger.debug("Indexed %s", url)
    except Exception:
        logger.debug("Failed to index %s (vector index unavailable or full)", url)


async def _index_batch_async(pages: list[dict]) -> None:
    """Fire-and-forget batch-index multiple pages.

    Ref: ADR-0030. For large crawls, this is ~200x faster than
    calling _index_page_async() per page.
    """
    if not pages:
        return
    try:
        from .semantic_client import SemanticClient

        settings = load_settings()
        semantic_url = settings.semantic_url
        client = SemanticClient(semantic_url)
        await client.index_batch(pages)
        await client.close()
        logger.debug("Batch-indexed %d pages", len(pages))
    except Exception:
        logger.debug(
            "Failed to batch-index %d pages (vector index unavailable)", len(pages)
        )
