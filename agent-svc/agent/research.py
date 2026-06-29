"""The agent research loop: search → scrape → think → answer.

Also provides the extract endpoint: scrape given URLs → LLM → structured data.
"""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from common.url import extract_domain

from .llm import LLMClient
from .scraper_client import ScraperClient
from .searxng_client import SearXNGClient

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are GroktoCrawl, a determined web research agent. Your job is to
thoroughly investigate each question by synthesizing information from the web
sources you have gathered. You are not a summarizer — you are a researcher.

IDENTITY
You are a research assistant who cares about getting the right answer. You weigh
evidence, identify patterns, detect contradictions, and flag uncertainty. You
are thorough and precise: you prefer specific, well-supported information over
vague generalizations, and you organise your findings so the reader can act on
them.

SOURCE QUALITY
Not all sources are equal. Evaluate each web page you draw from:
• Official documentation, academic / scientific publications, government or
  regulatory data, primary-source repositories — high authority.
• Established news outlets, technical reports by reputable organisations,
  industry analysts — medium authority.
• Personal / company blogs, forums, Q&A sites, social media — lower authority.
• Aggregators, clickbait, pages with no identifiable author or publication
  date — lowest authority.

When you must rely on lower-authority sources, say so explicitly. When several
independent, high-quality sources agree on a point, treat it as stronger
evidence. When they conflict, present both perspectives, assess the evidence
each side offers, and explain why the disagreement exists.

SYNTHESIS
• Look for consensus across the sources you have and highlight it.
• When sources contradict, present each viewpoint and compare the evidence.
• Note when the available sources are thin, one-sided, or incomplete.
• If the context does not contain enough information to answer the question
  fully, say so clearly and suggest what kind of sources would be needed for a
  complete answer.

INTEGRITY
• Base your answer ONLY on the web content provided in the context below.
  Do not use your own pre-training knowledge to fill gaps.
• Never fabricate information or invent sources. Every factual claim must be
  traceable to a specific source in the context.
• Cite sources by their URL whenever you use information from a specific page.

OUTPUT QUALITY
• Lead with the most important finding, then support it with evidence.
• Organise information clearly — use paragraphs, concise sections, or lists
  as appropriate.
• Be precise: specific numbers, names, and dates are better than general
  statements.
• For comparisons or trade-off analyses, present a balanced, point-by-point
  treatment.
• If structured output (JSON) is requested, gather your reasoning first, then
  format your final answer to match the requested schema exactly.
• Format your answer in clean markdown unless a JSON schema is provided."""

EXTRACT_SYSTEM_PROMPT = """You are GroktoCrawl, a structured data extraction agent.
Your job is to extract the requested information from the provided web content
as completely and accurately as possible.

Rules:
- Extract data based ONLY on the content provided below.
- If multiple instances of the requested data exist, extract ALL of them —
  do not stop after the first match.
- If a value is missing, incomplete, or ambiguous, note it rather than fabricating.
- If the content doesn't contain the requested information at all, return an
  empty result.
- If a schema is provided, respond with valid JSON matching that schema exactly.
- Organise extracted data clearly. If no schema is provided, format your answer
  in clean markdown with structure (tables, lists, sections as appropriate)."""

QUERY_INTELLIGENCE_SYSTEM_PROMPT = """You are a research planning agent. Given a user's research prompt, analyze what they need and produce a search plan.

Rules:
- For broad, multi-topic prompts, decompose into 3-6 specific search queries that each target a distinct sub-topic
- For narrow, single-topic prompts, use 1-2 queries and set strategy to "focused"
- Never pass the user's full prompt as a search query — extract the core search intent
- Output valid JSON only, no other text

Output format:
{
  "reasoning": "Brief analysis of what the user needs",
  "research_strategy": "deep" | "focused",
  "focused_queries": [
    "specific search query 1",
    "specific search query 2"
  ]
}"""


async def _generate_research_plan(
    prompt: str,
    llm: LLMClient,
) -> dict:
    """Phase 0: Analyze the user prompt with an LLM and produce a research plan.

    Returns a dict with keys:
        reasoning (str): brief analysis of what the user needs
        research_strategy (str): "deep" or "focused"
        focused_queries (list[str]): 1-6 specific search queries

    On any failure (API error, timeout, invalid JSON, empty response),
    falls back to using the prompt itself as a single focused query.
    """
    try:
        raw_response = await llm.generate(
            system_prompt=QUERY_INTELLIGENCE_SYSTEM_PROMPT,
            user_prompt=prompt,
        )

        # Strip markdown code fences if present
        cleaned = raw_response.strip()
        cleaned = cleaned.removeprefix("```json")
        cleaned = cleaned.removeprefix("```")
        cleaned = cleaned.removesuffix("```")
        cleaned = cleaned.strip()

        if not cleaned:
            raise ValueError("Empty response from query intelligence LLM")

        plan = json.loads(cleaned)

        # Validate required fields
        queries = plan.get("focused_queries", [prompt])
        if not isinstance(queries, list) or len(queries) == 0:
            queries = [prompt]

        strategy = plan.get("research_strategy", "focused")
        if strategy not in ("deep", "focused"):
            strategy = "deep" if len(queries) > 1 else "focused"

        return {
            "reasoning": plan.get("reasoning", ""),
            "research_strategy": strategy,
            "focused_queries": queries,
        }
    except Exception as e:
        logger.warning(
            "Query intelligence LLM call failed, falling back to verbatim prompt: %s",
            e,
        )
        return {
            "reasoning": "Query intelligence unavailable — using prompt verbatim",
            "research_strategy": "focused",
            "focused_queries": [prompt],
        }


async def _run_multi_query_discover_and_scrape(
    queries: list[str],
    urls: list[str] | None,
    searxng: SearXNGClient,
    scraper: ScraperClient,
    max_searches_per_request: int = 5,
    scrape_options: dict | None = None,
) -> dict:
    """Search multiple sub-queries, deduplicate URLs, scrape, and merge context.

    Iterates over ``queries`` (truncated to ``max_searches_per_request``),
    running a search for each. Collects all unique URLs across all queries
    (deduplicating by URL, keeping the first occurrence for richer metadata),
    then scrapes the union. Merges documents into a single context block
    organized by query.

    Returns the same dict shape as ``_run_research_discover_and_scrape()``:
        search_results, target_urls, documents, source_details, context
    """
    target_urls = list(urls) if urls else []
    all_search_results: list[dict] = []
    seen_urls: set[str] = set(target_urls)

    # Truncate to search budget
    budget = min(len(queries), max_searches_per_request)
    queries_to_run = queries[:budget]

    if not target_urls and queries_to_run:
        logger.info(
            "Multi-query research: running %d search queries (budget=%d)",
            len(queries_to_run),
            max_searches_per_request,
        )
        import asyncio as _asyncio

        search_tasks = [searxng.search(q, limit=10) for q in queries_to_run]
        search_results_list = await _asyncio.gather(
            *search_tasks, return_exceptions=True
        )
        for i, (query, result_tuple) in enumerate(  # type: ignore[misc]
            zip(queries_to_run, search_results_list, strict=False), start=1
        ):
            logger.info("  [%d/%d] Searching: %s", i, len(queries_to_run), query)
            if isinstance(result_tuple, Exception):
                logger.warning("Search failed for %s: %s", query, result_tuple)
                continue
            results, _health = result_tuple  # type: ignore[misc]
            for r in results:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_search_results.append(r)
                    target_urls.append(url)

    if not target_urls and not queries_to_run:
        return {
            "search_results": [],
            "target_urls": [],
            "documents": [],
            "source_details": [],
            "context": "",
        }

    # Score and rank URLs before scraping (F1: source pre-filtering)
    target_urls = _filter_and_rank_urls(target_urls, max_urls=20)

    preferred = [u for u in target_urls if not _is_video_platform_url(u)]
    deprioritized = [u for u in target_urls if _is_video_platform_url(u)]

    documents, source_details = await _scrape_urls(
        preferred,
        scraper,
        min_sources=3,
        max_attempts=len(preferred) or 10,
        scrape_options=scrape_options,
    )
    logger.info(
        "Multi-query: scraped %d docs from %d preferred URLs (attempts=%d)",
        len(documents),
        len(preferred),
        len(preferred) or 10,
    )

    if len(documents) < 3 and deprioritized:
        remaining = 3 - len(documents)
        extra_docs, extra_details = await _scrape_urls(
            deprioritized,
            scraper,
            min_sources=remaining,
            max_attempts=remaining * 2,
            scrape_options=scrape_options,
        )
        documents.extend(extra_docs)
        source_details.extend(extra_details)

    context = "\n\n---\n\n".join(documents) if documents else ""

    return {
        "search_results": all_search_results,
        "target_urls": target_urls,
        "documents": documents,
        "source_details": source_details,
        "context": context,
    }


async def _run_research_discover_and_scrape(
    prompt: str,
    urls: list[str] | None,
    searxng: SearXNGClient,
    scraper: ScraperClient,
    max_searches_per_request: int = 5,
    scrape_options: dict | None = None,
) -> dict:
    """Search → filter → scrape → context-building phase for research.

    Shared by ``run_research`` and ``run_research_stream``. Uses
    ``_scrape_urls()`` for batch scraping; the stream variant yields
    progress events from the returned source_details after the call.
    """
    target_urls = list(urls) if urls else []
    search_results: list[dict] = []
    if not target_urls:
        logger.info("No URLs provided. Searching for: %s", prompt)
        search_results, _health = await searxng.search(prompt, limit=10)
        target_urls = [r["url"] for r in search_results if r.get("url")]

    # Score and rank URLs before scraping (F1: source pre-filtering)
    target_urls = _filter_and_rank_urls(target_urls, max_urls=20)

    preferred = [u for u in target_urls if not _is_video_platform_url(u)]
    deprioritized = [u for u in target_urls if _is_video_platform_url(u)]

    documents, source_details = await _scrape_urls(
        preferred,
        scraper,
        min_sources=3,
        max_attempts=len(preferred) or 10,
        scrape_options=scrape_options,
    )
    logger.info(
        "run_research: scraped %d docs from %d preferred URLs (attempts=%d)",
        len(documents),
        len(preferred),
        len(preferred) or 10,
    )

    if len(documents) < 3 and deprioritized:
        remaining = 3 - len(documents)
        extra_docs, extra_details = await _scrape_urls(
            deprioritized,
            scraper,
            min_sources=remaining,
            max_attempts=remaining * 2,
            scrape_options=scrape_options,
        )
        documents.extend(extra_docs)
        source_details.extend(extra_details)

    context = "\n\n---\n\n".join(documents) if documents else ""

    return {
        "search_results": search_results,
        "target_urls": target_urls,
        "documents": documents,
        "source_details": source_details,
        "context": context,
    }


async def run_research(
    prompt: str,
    urls: list[str] | None = None,
    schema: dict | None = None,
    searxng_url: str = "http://searxng:8080",
    scraper_url: str = "http://scraper-svc:8001",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
    requested_model: str | None = None,
    max_searches_per_request: int = 5,
    include_images: bool = False,
) -> dict:
    """Execute the research loop: plan → search → scrape → think → answer.

    Phase 0: Query Intelligence analyzes the prompt and generates a research plan.
    Phase 1: Search (single or multi-query) and scrape.
    Phase 2: LLM synthesis with the scraped context.

    Multi-pass: After Phase 2, detects content gaps via LLM. If gaps are found
    and this is pass 1, a second pass searches the missing topics and re-synthesizes
    with the combined context. Capped at 2 passes.
    """
    searxng = SearXNGClient(searxng_url, max_searches=max_searches_per_request)
    scraper = ScraperClient(scraper_url)
    effective_model = (
        requested_model
        if requested_model and requested_model != "default"
        else llm_model
    )
    llm = LLMClient(llm_base_url, llm_api_key, effective_model)
    scrape_opts: dict | None = (
        {"formats": ["markdown", "images"]} if include_images else None
    )

    try:
        pass_count = 0
        max_passes = 1  # May increase to 2 if gaps are detected

        # Phase 0: Query Intelligence — analyze prompt, generate research plan (once)
        research_plan = await _generate_research_plan(prompt, llm)
        queries = research_plan["focused_queries"]
        strategy = research_plan["research_strategy"]

        all_source_details: list[dict] = []
        combined_context = ""
        gap_topics: list[str] = []

        while pass_count < max_passes:
            pass_count += 1

            if pass_count == 1:
                # ── Pass 1: normal discovery ──────────────────────
                if strategy == "deep" and len(queries) > 1:
                    discovered = await _run_multi_query_discover_and_scrape(
                        queries=queries,
                        urls=urls,
                        searxng=searxng,
                        scraper=scraper,
                        max_searches_per_request=max_searches_per_request,
                        scrape_options=scrape_opts,
                    )
                else:
                    query = queries[0] if queries else prompt
                    discovered = await _run_research_discover_and_scrape(
                        prompt=query,
                        urls=urls,
                        searxng=searxng,
                        scraper=scraper,
                        scrape_options=scrape_opts,
                    )
            else:
                # ── Pass 2: gap-focused discovery ─────────────────
                discovered = await _run_multi_query_discover_and_scrape(
                    queries=gap_topics,
                    urls=None,
                    searxng=searxng,
                    scraper=scraper,
                    max_searches_per_request=min(
                        len(gap_topics), max_searches_per_request
                    ),
                    scrape_options=scrape_opts,
                )

            context = discovered["context"]
            if not context and not combined_context:
                return {
                    "result": "I was unable to find or scrape any relevant web pages to answer your question.",
                    "sources": [],
                    "source_details": [],
                }

            # Combine contexts for multi-pass synthesis
            source_details = discovered["source_details"]
            all_source_details.extend(source_details)
            if pass_count == 1:
                combined_context = context
            else:
                if context:
                    combined_context = (
                        combined_context
                        + "\n\n---\n\nAdditional sources (pass 2):\n\n"
                        + context
                    )

            if not combined_context:
                return {
                    "result": "I was unable to find or scrape any relevant web pages to answer your question.",
                    "sources": [],
                    "source_details": [],
                }

            # ── Phase 2: Synthesis ────────────────────────────────
            answer = await llm.generate(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=prompt,
                context=combined_context,
                schema=schema,
            )
            _validate_json_if_schema(answer, schema)

            # ── Gap detection after pass 1 ─────────────────────────
            if pass_count == 1:
                gap_topics = await _detect_gaps(combined_context, llm)
                if not gap_topics:
                    break  # Coverage is adequate, done
                max_passes = 2  # Enable second pass

        return {
            "result": answer,
            "sources": [s["url"] for s in all_source_details],
            "source_details": all_source_details,
        }
    finally:
        await searxng.close()
        await scraper.close()
        await llm.close()


async def run_research_stream(
    prompt: str,
    urls: list[str] | None = None,
    schema: dict | None = None,
    searxng_url: str = "http://searxng:8080",
    scraper_url: str = "http://scraper-svc:8001",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
    requested_model: str | None = None,
    max_searches_per_request: int = 5,
    include_images: bool = False,
) -> AsyncGenerator[dict[str, Any], None]:
    """Streaming version of run_research. Yields SSE-suitable dicts.

    Phase 0 - Query Intelligence: analyze prompt, generate research plan (NEW).
    Phase 1 - Discovery: search (single or multi-query) + scrape, yielding progress events.
    Phase 2 - Synthesis: LLM token stream (or full response for schema-based output).

    Yields:
      {"type": "research_plan", "strategy": "...", "queries": [...], "reasoning": "..."} — plan
      {"type": "sources_pending", "sources": [...]} — search results (before scrape)
      {"type": "source_scraped", "url": "...", "source": "...", "chars": N} — each scraped page
      {"type": "token", "content": "..."} — individual tokens from the LLM (no schema)
      {"type": "done", "result": "...", "sources": [...], "latency_ms": N} — final
      {"type": "error", "content": "..."} — error
    """
    import time

    start = time.monotonic()

    searxng = SearXNGClient(searxng_url, max_searches=max_searches_per_request)
    scraper = ScraperClient(scraper_url)
    effective_model = (
        requested_model
        if requested_model and requested_model != "default"
        else llm_model
    )
    llm = LLMClient(llm_base_url, llm_api_key, effective_model)
    scrape_opts: dict | None = (
        {"formats": ["markdown", "images"]} if include_images else None
    )

    try:
        # Phase 0: Query Intelligence — analyze prompt, generate research plan
        yield {"type": "status", "state": "planning"}

        research_plan = await _generate_research_plan(prompt, llm)
        queries = research_plan["focused_queries"]
        strategy = research_plan["research_strategy"]
        reasoning = research_plan.get("reasoning", "")

        yield {
            "type": "research_plan",
            "strategy": strategy,
            "queries": queries,
            "reasoning": reasoning,
        }

        pass_count = 0
        max_passes = 1  # May increase to 2 if gaps are detected
        all_source_details: list[dict] = []
        combined_context = ""
        gap_topics: list[str] = []

        while pass_count < max_passes:
            pass_count += 1

            yield {
                "type": "research_pass",
                "pass": pass_count,
                "total_passes": max_passes,
            }
            yield {"type": "status", "state": "searching"}

            if pass_count == 1:
                # ── Pass 1: normal discovery ────────────────────────
                if strategy == "deep" and len(queries) > 1:
                    discovered = await _run_multi_query_discover_and_scrape(
                        queries=queries,
                        urls=urls,
                        searxng=searxng,
                        scraper=scraper,
                        max_searches_per_request=max_searches_per_request,
                        scrape_options=scrape_opts,
                    )
                else:
                    query = queries[0] if queries else prompt
                    discovered = await _run_research_discover_and_scrape(
                        prompt=query,
                        urls=urls,
                        searxng=searxng,
                        scraper=scraper,
                        scrape_options=scrape_opts,
                    )
            else:
                # ── Pass 2: gap-focused discovery ────────────────────
                discovered = await _run_multi_query_discover_and_scrape(
                    queries=gap_topics,
                    urls=None,
                    searxng=searxng,
                    scraper=scraper,
                    max_searches_per_request=min(
                        len(gap_topics), max_searches_per_request
                    ),
                    scrape_options=scrape_opts,
                )

            search_results = discovered["search_results"]
            source_details = discovered["source_details"]
            context = discovered["context"]

            # Yield pending sources for progress visibility
            pending_sources = (
                [
                    {
                        "url": r["url"],
                        "title": r.get("title", ""),
                        "relevance": r.get("description", ""),
                    }
                    for r in search_results
                    if r.get("url")
                ]
                if not urls
                else [{"url": u, "title": "", "relevance": ""} for u in urls]
            )
            yield {"type": "sources_pending", "sources": pending_sources}

            # Yield scraped source progress events
            for src in source_details:
                yield {
                    "type": "source_scraped",
                    "url": src["url"],
                    "source": src["source"],
                    "chars": src["char_count"],
                }

            if not context and not combined_context:
                elapsed = int((time.monotonic() - start) * 1000)
                yield {"type": "sources", "sources": []}
                yield {
                    "type": "done",
                    "result": (
                        "I was unable to find or scrape any relevant web pages."
                    ),
                    "sources": [],
                    "latency_ms": elapsed,
                }
                return

            # Combine contexts for multi-pass synthesis
            all_source_details.extend(source_details)
            if pass_count == 1:
                combined_context = context
            else:
                if context:
                    combined_context = (
                        combined_context
                        + "\n\n---\n\nAdditional sources (pass 2):\n\n"
                        + context
                    )

            if not combined_context:
                elapsed = int((time.monotonic() - start) * 1000)
                yield {"type": "sources", "sources": []}
                yield {
                    "type": "done",
                    "result": (
                        "I was unable to find or scrape any relevant web pages."
                    ),
                    "sources": [],
                    "latency_ms": elapsed,
                }
                return

            # Yield status heartbeat — synthesizing phase
            yield {"type": "status", "state": "synthesizing"}

            # Phase 2: Synthesis
            if schema:
                answer = await llm.generate(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=prompt,
                    context=combined_context,
                    schema=schema,
                )
                _validate_json_if_schema(answer, schema)

                # ── Gap detection after pass 1 ──────────────────────
                if pass_count == 1:
                    gap_topics = await _detect_gaps(combined_context, llm)
                    if not gap_topics:
                        break  # Coverage is adequate, done
                    max_passes = 2  # Enable second pass
                    continue
            else:
                # No schema — stream tokens from the LLM
                yield {
                    "type": "sources",
                    "sources": [s["url"] for s in all_source_details],
                }

                full_answer = ""
                async for event in llm.generate_stream(
                    system_prompt=SYSTEM_PROMPT,
                    user_prompt=prompt,
                    context=combined_context,
                ):
                    if event["type"] == "token":
                        full_answer += event["content"]
                        yield {"type": "token", "content": event["content"]}
                    elif event["type"] == "error":
                        yield {"type": "error", "content": event["content"]}
                        return
                    elif event["type"] == "done":
                        full_answer = event["full_content"]

                # ── Gap detection after pass 1 ──────────────────────
                if pass_count == 1:
                    gap_topics = await _detect_gaps(combined_context, llm)
                    if not gap_topics:
                        break  # Coverage is adequate, done
                    max_passes = 2  # Enable second pass
                    continue

        # ── Final done event after all passes ───────────────────────
        source_list = [s["url"] for s in all_source_details]
        elapsed = int((time.monotonic() - start) * 1000)

        if schema:
            yield {"type": "sources", "sources": source_list}
            yield {
                "type": "done",
                "result": answer,
                "sources": source_list,
                "latency_ms": elapsed,
            }
        else:
            yield {
                "type": "done",
                "result": full_answer,
                "sources": source_list,
                "latency_ms": elapsed,
            }

    finally:
        await searxng.close()
        await scraper.close()
        await llm.close()


async def run_extract(
    urls: list[str],
    prompt: str | None = None,
    schema: dict | None = None,
    scraper_url: str = "http://scraper-svc:8001",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
) -> dict:
    """Extract structured data from given URLs. No search step."""
    scraper = ScraperClient(scraper_url)
    llm = LLMClient(llm_base_url, llm_api_key, llm_model)

    try:
        documents, source_details = await _scrape_urls(urls, scraper)
        context = "\n\n---\n\n".join(documents) if documents else ""

        if not context:
            return {
                "result": "No content could be extracted from the provided URLs.",
                "sources": [],
                "source_details": [],
            }

        user_prompt = (
            prompt or "Extract the requested information from the provided content."
        )
        answer = await llm.generate(
            system_prompt=EXTRACT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            context=context,
            schema=schema,
        )
        _validate_json_if_schema(answer, schema)
        return {
            "result": answer,
            "sources": [s["url"] for s in source_details],
            "source_details": source_details,
        }
    finally:
        await scraper.close()
        await llm.close()


# ── Enrich pipeline ──────────────────────────────────────────────

ENRICH_SYSTEM_PROMPT = """You are a precise structured data extractor. Your job is to extract
specific fields from the provided web page content and return them as JSON.

RULES:
- Extract data based ONLY on the content provided below.
- For each field, return a JSON object with "value" and "source_url" keys.
- "value" should be the extracted information as a string, or null if not found.
- "source_url" should be the original URL of the page (provided in the context).
- If the information for a field is not present in the content, set value to null.
- Do not fabricate information. Do not hallucinate values.
- Return ONLY valid JSON, no markdown or explanation."""


async def run_enrich_pipeline(
    items: list[dict],
    fields: dict,
    source_hint: str | None = None,
    effort: str = "low",
    searxng_url: str = "http://searxng:8080",
    scraper_url: str = "http://scraper-svc:8001",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
) -> list[dict]:
    """Enrich a list of entities with web-sourced structured data.

    Each item is processed independently: search → scrape → LLM extraction.
    Items are processed in parallel with a bounded semaphore.

    Args:
        items: List of entity dicts to enrich (e.g., [{"company": "Anthropic"}]).
        fields: Dict of field_name → EnrichmentField (with description).
        source_hint: Optional hint to guide search (company, person, url, product).
        effort: Controls search depth — "low" (3 results), "medium"/"high" (5).
        searxng_url: SearXNG API base URL.
        scraper_url: Scraper service base URL.
        llm_base_url: LLM API base URL.
        llm_api_key: LLM API key.
        llm_model: LLM model name.

    Returns:
        List of dicts with keys: ``item`` (original item) and ``enrichments``
        (dict of field_name → {value, source}).
    """
    import asyncio

    search_limit = 3 if effort == "low" else 5
    semaphore = asyncio.Semaphore(3)

    async def enrich_one(item: dict) -> dict:
        async with semaphore:
            searxng = SearXNGClient(searxng_url)
            scraper = ScraperClient(scraper_url)
            llm = LLMClient(llm_base_url, llm_api_key, llm_model)
            try:
                # Build search query from item values + source_hint
                search_parts = [str(v) for v in item.values() if v is not None]
                if source_hint:
                    search_parts.append(source_hint)
                query = " ".join(search_parts) if search_parts else ""

                if not query:
                    return {"item": item, "enrichments": {}}

                # Search
                logger.info("Enrich: searching for: %s", query)
                results, _health = await searxng.search(query, limit=search_limit)

                if not results:
                    logger.info("Enrich: no results for: %s", query)
                    return {"item": item, "enrichments": {}}

                # Scrape top result
                top_url = results[0]["url"]
                logger.info("Enrich: scraping: %s", top_url)
                scraped = await scraper.scrape(top_url)

                markdown = (
                    scraped.get("data", {}).get("markdown", "")
                    if scraped.get("success")
                    else ""
                )
                if not markdown:
                    return {"item": item, "enrichments": {}}

                # LLM extraction
                field_descriptions = "\n".join(
                    f"- {name}: {field.description}" for name, field in fields.items()
                )
                extract_prompt = (
                    "Extract the following fields from the text below.\n"
                    "Return ONLY a JSON object where keys are field names "
                    'and values are objects with "value" and "source_url".\n\n'
                    f"Source URL: {top_url}\n\n"
                    f"Fields to extract:\n{field_descriptions}\n\n"
                    f"---TEXT---\n{markdown[:8000]}"
                )

                try:
                    result_text = await llm.generate(
                        system_prompt=ENRICH_SYSTEM_PROMPT,
                        user_prompt=extract_prompt,
                    )
                    extracted = _parse_enrich_json(result_text)
                except Exception:
                    logger.warning(
                        "Enrich: LLM extraction failed for: %s", query, exc_info=True
                    )
                    extracted = {}

                enrichments: dict[str, dict[str, str | None]] = {}
                for name in fields:
                    val = extracted.get(name, {})
                    if isinstance(val, dict):
                        enrichments[name] = {
                            "value": val.get("value"),
                            "source": val.get("source_url", top_url),
                        }
                    else:
                        enrichments[name] = {
                            "value": str(val) if val is not None else None,
                            "source": top_url,
                        }

                return {"item": item, "enrichments": enrichments}
            finally:
                await searxng.close()
                await scraper.close()
                await llm.close()

    tasks = [enrich_one(item) for item in items]
    results = await asyncio.gather(*tasks)
    return list(results)


def _parse_enrich_json(text: str) -> dict:
    """Parse JSON from an LLM enrichment response, handling markdown wrapping."""
    import json as _json

    try:
        return _json.loads(text.strip())
    except (_json.JSONDecodeError, Exception):
        pass

    # Extract JSON block if wrapped in markdown
    if "```json" in text:
        try:
            block = text.split("```json")[1].split("```")[0].strip()
            return _json.loads(block)
        except (_json.JSONDecodeError, Exception):
            pass
    elif "```" in text:
        try:
            block = text.split("```")[1].split("```")[0].strip()
            return _json.loads(block)
        except (_json.JSONDecodeError, Exception):
            pass

    logger.warning("Enrich: failed to parse LLM JSON response")
    return {}


async def _scrape_urls(
    urls: list[str],
    scraper: ScraperClient,
    min_sources: int = 3,
    max_attempts: int | None = None,
    max_concurrent: int = 5,
    scrape_options: dict | None = None,
) -> tuple[list[str], list[dict]]:
    """Scrape URLs with bounded concurrency and return (documents, source_details).

    Tries URLs in batches until ``min_sources`` are successfully scraped
    or the list is exhausted (whichever comes first).
    Uses a semaphore (default ``max_concurrent`` = 5) with per-URL timeout (20s).
    ``max_attempts`` sets an upper bound on how many URLs are tried.
    """
    import asyncio

    documents: list[str] = []
    source_details: list[dict] = []
    max_attempts = max_attempts or len(urls)
    semaphore = asyncio.Semaphore(max_concurrent)
    url_timeout = 70  # Accommodates scrape_with_fallback (20s generic + 45s browser)

    async def _scrape_one(url: str) -> tuple[str | None, dict | None]:
        async with semaphore:
            try:
                logger.info("Scraping: %s", url)
                result = await asyncio.wait_for(
                    scraper.scrape_with_fallback(url, scrape_options=scrape_options),
                    timeout=url_timeout,
                )
                if result.get("success") and result.get("data", {}).get("markdown"):
                    md = result["data"]["markdown"]
                    domain = extract_domain(url)
                    doc = f"Source: {url} (domain: {domain})\n\n{md[:8000]}"
                    src = {
                        "url": url,
                        "source": result["data"].get("source", "unknown"),
                        "char_count": len(md),
                    }
                    return doc, src
                else:
                    logger.warning("Failed to scrape %s: %s", url, result.get("error"))
                    return None, None
            except TimeoutError:
                logger.warning("Timeout scraping %s after %ss", url, url_timeout)
                return None, None
            except Exception as e:
                logger.warning("Error scraping %s: %s", url, e)
                return None, None

    # Process URLs in batches — launch concurrent tasks, collect results,
    # stop when min_sources is reached or max_attempts exhausted
    pending = list(urls)
    task_to_url: dict[asyncio.Task, str] = {}
    tasks: set[asyncio.Task] = set()
    attempts = 0

    while pending or tasks:
        # Fill slots up to our budget
        while len(tasks) < max_concurrent and pending and attempts < max_attempts:
            url = pending.pop(0)
            attempts += 1
            task = asyncio.create_task(_scrape_one(url))
            task_to_url[task] = url
            tasks.add(task)

        if not tasks:
            break

        # Wait for at least one task to complete
        done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in done:
            doc, src = task.result()
            if doc and src:
                documents.append(doc)
                source_details.append(src)
                if len(documents) >= min_sources:
                    # Cancel remaining tasks and stop
                    for t in tasks:
                        t.cancel()
                    tasks.clear()
                    return documents, source_details

    return documents, source_details


def _validate_json_if_schema(answer: str, schema: dict | None) -> None:
    """If a schema was provided, attempt to parse the answer as JSON."""
    if not schema:
        return
    try:
        cleaned = answer.strip()
        cleaned = cleaned.removeprefix("```json")
        cleaned = cleaned.removeprefix("```")
        cleaned = cleaned.removesuffix("```")
        json.loads(cleaned)
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("LLM response not valid JSON despite schema: %s", e)


# ── Video-platform URL filtering ────────────────────────────────
# Domains whose primary content is audio-visual (video, audio,
# short-form) rather than text.  Transcripts extracted from these
# platforms are low-signal for factual text queries and pollute the
# LLM context.  They are deprioritised — only used as a fallback
# when text sources can't fill the ``min_sources`` quota.
_VIDEO_PLATFORM_DOMAINS: frozenset[str] = frozenset(
    {
        "youtube.com",
        "youtu.be",
        "m.youtube.com",
        "tiktok.com",
        "www.tiktok.com",
        "vm.tiktok.com",
        "instagram.com",
        "www.instagram.com",
    }
)


def _is_video_platform_url(url: str) -> bool:
    """Return True when *url* belongs to a video-first platform."""
    hostname = extract_domain(url).lower()
    # Strip leading "www." for comparison (the frozenset includes
    # both canonicalised and www-prefixed variants).
    return (
        hostname in _VIDEO_PLATFORM_DOMAINS
        or hostname.removeprefix("www.") in _VIDEO_PLATFORM_DOMAINS
    )


# ── URL scoring and pre-filtering ────────────────────────────────
# Each URL discovered by search is scored for expected extractability
# before scraping. Low-value URLs (login, checkout, index pages) are
# excluded or deprioritised to maximise the scrape budget.


def _score_url(url: str) -> int:
    """Score a URL's expected extractability. Higher = more likely to yield content."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    path = parsed.path.lower()

    # ── Exclude immediately ────────────────────────────────────
    skip_paths = (
        "/login",
        "/signup",
        "/cart",
        "/checkout",
        "/terms",
        "/privacy",
        "/tag/",
    )
    if any(p in path for p in skip_paths):
        return -9999

    # ── Deprioritise tracking-param URLs ───────────────────────
    if "utm_" in parsed.query:
        return -5000

    score = 0

    # ── Domain authority boosts ────────────────────────────────
    if hostname.endswith(".edu"):
        score += 2
    if "wikipedia.org" in hostname:
        score += 2
    if "github.com" in hostname:
        score += 2
    if "youtube.com" in hostname or "youtu.be" in hostname:
        score += 2

    # Known high-quality domains (gardening, health, academic, news)
    _high_quality = frozenset(
        {
            "provenwinners.com",
            "logees.com",
            "almanac.com",
            "nhs.uk",
            "mayoclinic.org",
            "webmd.com",
            "sciencedirect.com",
            "scholar.google.com",
            "acm.org",
            "ieee.org",
            "arstechnica.com",
            "reuters.com",
            "apnews.com",
            "npr.org",
        }
    )
    if any(d in hostname for d in _high_quality):
        score += 2
    if hostname.endswith(".gov") or hostname.endswith(".org"):
        score += 1

    # Established blogs / developer sites
    _known_blogs = frozenset({"medium.com", "dev.to", "smashingmagazine.com"})
    if any(d in hostname for d in _known_blogs):
        score += 1

    # ── Penalise social media / aggregators ────────────────────
    _low_quality = frozenset(
        {
            "reddit.com",
            "pinterest.com",
            "facebook.com",
            "twitter.com",
            "x.com",
            "linkedin.com",
            "tumblr.com",
            "quora.com",
            "stackexchange.com",
        }
    )
    if any(d in hostname for d in _low_quality):
        score -= 1

    # ── Prefer specific article pages over index/root ──────────
    if path.count("/") >= 2:
        score += 1
    elif path in ("", "/"):
        score -= 1

    return score


def _filter_and_rank_urls(urls: list[str], max_urls: int = 20) -> list[str]:
    """Score, sort, filter URLs and return the top N."""
    scored = [(url, _score_url(url)) for url in urls]
    # Exclude skip-score URLs
    scored = [(url, s) for url, s in scored if s > -1000]
    # Sort by score descending
    scored.sort(key=lambda x: x[1], reverse=True)
    return [url for url, _ in scored[:max_urls]]


async def _detect_gaps(combined_context: str, llm: LLMClient) -> list[str]:
    """Check if the research context has coverage gaps.

    Uses an LLM call to analyze the scraped context for gap signals:
    topics that are mentioned as missing, not covered, or insufficiently
    documented in the gathered sources.

    Returns a list of topic strings (max 5) for missing areas, or an
    empty list if coverage is adequate.
    """
    if not combined_context:
        return []

    gap_check_prompt = (
        "Analyze the following research context and identify specific topics "
        "that are mentioned as missing, not covered, or insufficiently "
        "documented. Return a JSON array of topic strings (max 5). "
        "Return [] if coverage is adequate.\n\n"
        f"Context:\n{combined_context[:4000]}"
    )
    try:
        result = await llm.generate(
            system_prompt="You are a research gap analyzer.",
            user_prompt=gap_check_prompt,
        )
        cleaned = result.strip().removeprefix("```json").removesuffix("```").strip()
        gaps = json.loads(cleaned)
        if isinstance(gaps, list) and len(gaps) <= 5:
            return [g for g in gaps if isinstance(g, str)]
        return []
    except (json.JSONDecodeError, Exception) as e:
        logger.warning("Gap detection failed: %s", e)
        return []


ANSWER_SYSTEM_PROMPT = """You are GroktoCrawl, a helpful Q&A agent. Your job is to answer
the user's question using ONLY the web search results provided below.

RULES:
- Base your answer ONLY on the context provided. Do not use your pre-training knowledge.
- Cite sources using inline markers like [1], [2], etc. Each marker corresponds to a source URL listed below.
- If the context doesn't contain enough information to answer fully, say so clearly.
- Be concise but thorough. Lead with the direct answer, then add supporting detail.
- Use clean markdown formatting.
- Do not fabricate information or invent sources."""


async def _rerank_answer_sources(
    search_results: list[dict],
    query: str,
    retrieval_mode: str,
    semantic_url: str,
    scraper_url: str,
    limit: int,
) -> list[dict]:
    """Rerank or augment search results for the answer pipeline."""
    if retrieval_mode == "keyword" or not search_results:
        return search_results

    from .scraper_client import ScraperClient
    from .semantic_client import SemanticClient

    semantic = SemanticClient(semantic_url)
    scraper = ScraperClient(scraper_url)
    try:
        if retrieval_mode in ("semantic", "hybrid"):
            urls_to_scrape = [r["url"] for r in search_results[:limit]]
            contents = []
            for url in urls_to_scrape:
                try:
                    scraped = await scraper.scrape(url)
                    c = (
                        scraped.get("data", {}).get("markdown", "")
                        if scraped.get("success")
                        else ""
                    )
                    contents.append(c[:2000])
                except Exception:
                    contents.append("")

            if retrieval_mode == "semantic":
                embeddings = await semantic.embed([query, *contents])
                similarities = [
                    sum(a * b for a, b in zip(embeddings[0], emb, strict=False))
                    for emb in embeddings[1:]
                ]
                ranked = sorted(
                    range(len(similarities)),
                    key=lambda i: similarities[i],
                    reverse=True,
                )
                return [search_results[i] for i in ranked if i < len(search_results)]
            else:
                reranked = await semantic.rerank(
                    query,
                    [r.get("description", "") for r in search_results[:limit]],
                    top_k=limit,
                )
                new_order = [item["index"] for item in reranked]
                return [search_results[i] for i in new_order if i < len(search_results)]

        elif retrieval_mode == "vector":
            vector_results = await semantic.search_vector(query, limit=limit)
            return [
                {"url": r["url"], "title": r["title"], "description": ""}
                for r in vector_results
            ]

        elif retrieval_mode == "hybrid_vector":
            vector_results = await semantic.search_vector(query, limit=limit)
            seen: set[str] = set()
            merged: list[dict] = []
            for r in search_results[:limit]:
                if r["url"] not in seen:
                    seen.add(r["url"])
                    merged.append(r)
            for r in vector_results:
                if r["url"] not in seen:
                    seen.add(r["url"])
                    merged.append(
                        {"url": r["url"], "title": r["title"], "description": ""}
                    )
            return merged[:limit]

        return search_results
    finally:
        await semantic.close()
        await scraper.close()


async def _run_answer_discover_and_scrape(
    query: str,
    num_sources: int,
    retrieval_mode: str,
    searxng: SearXNGClient,
    scraper: ScraperClient,
    semantic_url: str,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    requested_model: str | None,
    max_searches_per_request: int = 5,
) -> dict:
    """Search → rerank → filter → scrape → context-building for answer.

    Shared by ``run_answer`` and ``run_answer_stream``. Returns all
    intermediate data needed by both callers to proceed to LLM synthesis
    and citation parsing.
    """
    search_results: list[dict] = []
    logger.info("Answer: searching for: %s", query)
    search_results, _health = await searxng.search(query, limit=num_sources * 2)

    if retrieval_mode != "keyword":
        search_results = await _rerank_answer_sources(
            search_results,
            query,
            retrieval_mode,
            semantic_url,
            scraper.base_url,
            num_sources,
        )

    target_urls = [r["url"] for r in search_results if r.get("url")]

    # Step 2: Scrape (prefer text sources, use video platforms as fallback)
    preferred = [u for u in target_urls if not _is_video_platform_url(u)]
    deprioritized = [u for u in target_urls if _is_video_platform_url(u)]

    if deprioritized:
        logger.info(
            "Answer: %d preferred + %d video-platform URLs (deprioritized)",
            len(preferred),
            len(deprioritized),
        )

    documents, source_details = await _scrape_urls(
        preferred,
        scraper,
        min_sources=num_sources,
        max_attempts=num_sources * 2,
    )

    if len(documents) < num_sources and deprioritized:
        logger.info(
            "Answer: %d/%d from preferred sources, falling back to video-platform URLs",
            len(documents),
            num_sources,
        )
        remaining = num_sources - len(documents)
        more_docs, more_details = await _scrape_urls(
            deprioritized,
            scraper,
            min_sources=remaining,
            max_attempts=remaining * 2,
        )
        documents.extend(more_docs)
        source_details.extend(more_details)

    # Step 3: Build context with source markers
    context_parts = []
    for i, (doc, detail) in enumerate(
        zip(documents, source_details, strict=False), start=1
    ):
        url = detail["url"]
        title = next(
            (r.get("title", "") for r in search_results if r.get("url") == url), ""
        )
        context_parts.append(f"[{i}] Source: {url}\nTitle: {title}\n\n{doc}")

    context = "\n\n---\n\n".join(context_parts) if context_parts else ""

    # Step 4: Build source_map for citation resolution
    source_map: list[dict[str, str]] = []
    for r in search_results:
        if r.get("url") in [s["url"] for s in source_details]:
            source_map.append(
                {
                    "url": r["url"],
                    "title": r.get("title", ""),
                    "relevance": r.get("description", ""),
                }
            )

    return {
        "search_results": search_results,
        "context_parts": context_parts,
        "documents": documents,
        "source_details": source_details,
        "context": context,
        "source_map": source_map,
    }


async def run_answer(
    query: str,
    num_sources: int = 5,
    search_type: str = "auto",
    retrieval_mode: str = "keyword",
    searxng_url: str = "http://searxng:8080",
    scraper_url: str = "http://scraper-svc:8001",
    semantic_url: str = "http://semantic-svc:8003",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
    requested_model: str | None = None,
    max_searches_per_request: int = 5,
) -> dict:
    """Run a grounded Q&A pipeline: search → scrape → LLM → citations.

    Returns a dict with keys: answer, sources (list of dicts), citations (list of dicts),
    search_type, latency_ms.
    """
    import time

    start = time.monotonic()

    searxng = SearXNGClient(searxng_url, max_searches=max_searches_per_request)
    scraper = ScraperClient(scraper_url)
    effective_model = (
        requested_model
        if requested_model and requested_model != "default"
        else llm_model
    )
    llm = LLMClient(llm_base_url, llm_api_key, effective_model)

    try:
        discovered = await _run_answer_discover_and_scrape(
            query=query,
            num_sources=num_sources,
            retrieval_mode=retrieval_mode,
            searxng=searxng,
            scraper=scraper,
            semantic_url=semantic_url,
            llm_base_url=llm_base_url,
            llm_api_key=llm_api_key,
            llm_model=llm_model,
            requested_model=requested_model,
        )

        context = discovered["context"]
        source_map = discovered["source_map"]

        if not context:
            elapsed = int((time.monotonic() - start) * 1000)
            return {
                "answer": "I was unable to find or scrape any relevant web pages to answer your question.",
                "sources": [],
                "citations": [],
                "search_type": search_type,
                "latency_ms": elapsed,
            }

        # Call LLM
        user_prompt = (
            f"Answer the following question using ONLY the sources provided above.\n\n"
            f"Question: {query}\n\n"
            f"Cite sources using [1], [2], etc. corresponding to the source numbers above."
        )
        answer = await llm.generate(
            system_prompt=ANSWER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            context=context,
        )

        # Parse citations [N] from the answer
        citations: list[dict] = []
        seen_indices: set[int] = set()
        import re

        for match in re.finditer(r"\[(\d+)\]", answer):
            idx = int(match.group(1))
            if idx not in seen_indices and 1 <= idx <= len(source_map):
                seen_indices.add(idx)
                citations.append({"index": idx, "url": source_map[idx - 1]["url"]})

        elapsed = int((time.monotonic() - start) * 1000)

        return {
            "answer": answer,
            "sources": source_map,
            "citations": citations,
            "search_type": search_type,
            "latency_ms": elapsed,
        }
    finally:
        await searxng.close()
        await scraper.close()
        await llm.close()


async def run_answer_stream(
    query: str,
    num_sources: int = 5,
    search_type: str = "auto",
    retrieval_mode: str = "keyword",
    searxng_url: str = "http://searxng:8080",
    scraper_url: str = "http://scraper-svc:8001",
    semantic_url: str = "http://semantic-svc:8003",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
    requested_model: str | None = None,
    max_searches_per_request: int = 5,
) -> AsyncGenerator[dict[str, Any], None]:
    """Streaming version of run_answer. Yields SSE-suitable dicts.

    Yields:
      {"type": "sources", "sources": [...]} — source list (sent once before tokens)
      {"type": "token", "content": "..."} — individual tokens from the LLM
      {"type": "done", "answer": "...", "citations": [...], "latency_ms": N} — final
      {"type": "error", "content": "..."} — error
    """
    import time

    start = time.monotonic()

    searxng = SearXNGClient(searxng_url, max_searches=max_searches_per_request)
    scraper = ScraperClient(scraper_url)
    effective_model = (
        requested_model
        if requested_model and requested_model != "default"
        else llm_model
    )
    llm = LLMClient(llm_base_url, llm_api_key, effective_model)

    try:
        # Step 1: Search (fetch extra results to allow for scrape failures)
        logger.info("Answer (stream): searching for: %s", query)
        search_results, _health = await searxng.search(query, limit=num_sources * 2)

        if retrieval_mode != "keyword":
            search_results = await _rerank_answer_sources(
                search_results,
                query,
                retrieval_mode,
                semantic_url,
                scraper_url,
                num_sources,
            )
        target_urls = [r["url"] for r in search_results if r.get("url")]

        # Yield pending sources for progress visibility
        pending_sources = [
            {
                "url": r["url"],
                "title": r.get("title", ""),
                "relevance": r.get("description", ""),
            }
            for r in search_results
            if r.get("url")
        ]
        yield {"type": "sources_pending", "sources": pending_sources}

        # Step 2: Scrape (prefer text sources, use video platforms as fallback)
        preferred = [u for u in target_urls if not _is_video_platform_url(u)]
        deprioritized = [u for u in target_urls if _is_video_platform_url(u)]

        if deprioritized:
            logger.info(
                "Answer (stream): %d preferred + %d video-platform URLs (deprioritized)",
                len(preferred),
                len(deprioritized),
            )

        documents, source_details = await _scrape_urls(
            preferred,
            scraper,
            min_sources=num_sources,
            max_attempts=num_sources * 2,
        )

        if len(documents) < num_sources and deprioritized:
            logger.info(
                "Answer (stream): %d/%d from preferred sources, falling back to video-platform URLs",
                len(documents),
                num_sources,
            )
            remaining = num_sources - len(documents)
            more_docs, more_details = await _scrape_urls(
                deprioritized,
                scraper,
                min_sources=remaining,
                max_attempts=remaining * 2,
            )
            documents.extend(more_docs)
            source_details.extend(more_details)

        # Step 3: Build context
        context_parts = []
        source_map: list[dict[str, str]] = []
        for i, (doc, detail) in enumerate(
            zip(documents, source_details, strict=False), start=1
        ):
            url = detail["url"]
            title = next(
                (r.get("title", "") for r in search_results if r.get("url") == url), ""
            )
            context_parts.append(f"[{i}] Source: {url}\nTitle: {title}\n\n{doc}")
            source_map.append(
                {
                    "url": url,
                    "title": title,
                    "relevance": next(
                        (
                            r.get("description", "")
                            for r in search_results
                            if r.get("url") == url
                        ),
                        "",
                    ),
                }
            )

        context = "\n\n---\n\n".join(context_parts) if context_parts else ""

        if not context:
            yield {"type": "sources", "sources": []}
            yield {
                "type": "done",
                "answer": "No relevant web pages found.",
                "citations": [],
                "latency_ms": int((time.monotonic() - start) * 1000),
            }
            return

        # Yield sources before streaming tokens
        yield {"type": "sources", "sources": source_map}

        # Step 4: Stream LLM response
        user_prompt = (
            f"Answer the following question using ONLY the sources provided above.\n\n"
            f"Question: {query}\n\n"
            f"Cite sources using [1], [2], etc. corresponding to the source numbers above."
        )
        full_answer = ""
        async for event in llm.generate_stream(
            system_prompt=ANSWER_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            context=context,
        ):
            if event["type"] == "token":
                full_answer += event["content"]
                yield {"type": "token", "content": event["content"]}
            elif event["type"] == "error":
                yield {"type": "error", "content": event["content"]}
                return
            elif event["type"] == "done":
                full_answer = event["full_content"]

        # Step 5: Parse citations
        import re

        citations: list[dict] = []
        seen_indices: set[int] = set()
        for match in re.finditer(r"\[(\d+)\]", full_answer):
            idx = int(match.group(1))
            if idx not in seen_indices and 1 <= idx <= len(source_map):
                seen_indices.add(idx)
                citations.append({"index": idx, "url": source_map[idx - 1]["url"]})

        elapsed = int((time.monotonic() - start) * 1000)
        yield {
            "type": "done",
            "answer": full_answer,
            "citations": citations,
            "latency_ms": elapsed,
        }

    finally:
        await searxng.close()
        await scraper.close()
        await llm.close()


# ── Content extraction helpers ──────────────────────────────────

HIGHLIGHTS_SYSTEM_PROMPT = """You are a precise passage extractor. Your job is to identify and
extract the most relevant passages from the provided text that match a given query.

RULES:
- Extract ONLY passages that are present verbatim (or very close to verbatim) in the source text.
- Do not paraphrase, summarize, or add commentary.
- Return the passages separated by blank lines, in order of relevance.
- If no passages are relevant, return an empty string.
- Keep the total output within the character limit."""

SUMMARY_SYSTEM_PROMPT = """You are a concise summarizer. Your job is to produce a brief, accurate
summary of the provided text, optionally focusing on a specific query.

RULES:
- Base your summary ONLY on the provided text. Do not add external knowledge.
- Be concise — aim for the requested token budget.
- Lead with the most important point.
- Use plain text, no markdown formatting."""


async def extract_highlights(
    text: str,
    query: str | None,
    max_chars: int,
    llm_client,
) -> str:
    """Extract the most relevant passages from text matching the query.

    Uses the LLM to identify and extract verbatim or near-verbatim passages
    that are most relevant to the query. Returns up to max_chars characters.

    Graceful degradation: returns empty string on LLM failure.

    Args:
        text: The scraped page content (markdown).
        query: Search query to match passages against (optional, defaults to "the main topic").
        max_chars: Maximum characters to return.
        llm_client: An LLMClient instance for generating responses.

    Returns:
        Extracted passages as a string, or empty string on failure.
    """
    if not text or not text.strip():
        return ""

    effective_query = query or "the main topic"
    truncated = text[:10000]

    try:
        user_prompt = (
            f"From the text below, extract the passages most relevant to: {effective_query}\n"
            f"Return up to {max_chars} characters. Return only the extracted passages, "
            f"no commentary."
        )
        result = await llm_client.generate(
            system_prompt=HIGHLIGHTS_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            context=truncated,
        )
        # Trim to requested max characters
        if len(result) > max_chars:
            result = result[:max_chars].rsplit(" ", 1)[0]
        return result.strip()
    except Exception as e:
        logger.warning("extract_highlights failed: %s", e)
        return ""


async def extract_summary(
    text: str,
    query: str | None,
    max_tokens: int,
    llm_client,
) -> str:
    """Generate a concise summary of the text.

    Uses the LLM to produce a brief summary, optionally focused on a query.

    Graceful degradation: returns empty string on LLM failure.

    Args:
        text: The scraped page content (markdown).
        query: Optional focus for the summary.
        max_tokens: Approximate token budget for the summary.
        llm_client: An LLMClient instance for generating responses.

    Returns:
        Summary text as a string, or empty string on failure.
    """
    if not text or not text.strip():
        return ""

    focus_clause = f" with focus on: {query}" if query else ""
    truncated = text[:10000]

    try:
        user_prompt = (
            f"Summarize the text below{focus_clause}.\n"
            f"Keep it to {max_tokens} tokens or fewer."
        )
        result = await llm_client.generate(
            system_prompt=SUMMARY_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            context=truncated,
        )
        return result.strip()
    except Exception as e:
        logger.warning("extract_summary failed: %s", e)
        return ""


async def process_contents_for_results(
    results: list[dict],
    query: str,
    contents_options,
    llm_client,
    scraper_client,
) -> list[dict]:
    """Apply contents options (highlights, summary, extras) to search results.

    Scrapes each result URL, then applies per-result LLM highlights and/or
    summaries based on the contents_options configuration. Handles extras
    extraction by passing contents to the scraper-svc.

    Graceful degradation: LLM failures return empty strings. Scraping failures
    skip the result.

    Args:
        results: List of search result dicts (each has url, title, description).
        query: The original search query.
        contents_options: ContentsOptions from the request.
        llm_client: An LLMClient instance.
        scraper_client: A ScraperClient instance.

    Returns:
        List of enriched result dicts with additional keys: highlights, summary,
        extras, markdown.
    """
    import asyncio

    enriched: list[dict] = []

    highlights_opts: dict = {}
    summary_opts: dict = {}
    want_highlights = False
    want_summary = False

    if contents_options.highlights is not None:
        want_highlights = True
        if isinstance(contents_options.highlights, dict):
            highlights_opts = contents_options.highlights

    if contents_options.summary is not None:
        want_summary = True
        if isinstance(contents_options.summary, dict):
            summary_opts = contents_options.summary

    want_extras = contents_options.extras is not None

    # Build scraper request body — include contents for extras extraction
    scraper_body: dict = {"url": ""}
    if want_extras:
        scraper_body["contents"] = {
            "extras": contents_options.extras.model_dump(exclude_none=True)
        }

    semaphore = asyncio.Semaphore(2)

    async def _process_one(result: dict) -> dict:
        url = result.get("url", "")
        entry = dict(result)  # Copy
        if not url:
            return entry

        async with semaphore:
            # Scrape the URL
            try:
                scraper_body["url"] = url
                scraped = await asyncio.wait_for(
                    scraper_client._client.post(
                        f"{scraper_client.base_url}/scrape",
                        json=scraper_body,
                    ),
                    timeout=30,
                )
                scraped_data = scraped.json()
            except Exception as e:
                logger.warning("Failed to scrape %s for contents: %s", url, e)
                return entry

            if not scraped_data.get("success"):
                return entry

            data = scraped_data.get("data", {})
            markdown = data.get("markdown", "")

            if want_extras:
                entry["extras"] = data.get("extras")

            if not markdown:
                return entry

            entry["markdown"] = markdown

            # ── Highlights ──────────────────────────────────
            if want_highlights:
                hq = highlights_opts.get("query") or query
                hmax = highlights_opts.get("maxCharacters", 500)
                try:
                    entry["highlights"] = await extract_highlights(
                        markdown, hq, hmax, llm_client
                    )
                except Exception:
                    entry["highlights"] = ""

            # ── Summary ─────────────────────────────────────
            if want_summary:
                sq = summary_opts.get("query") or query
                smax = summary_opts.get("maxTokens", 150)
                try:
                    entry["summary"] = await extract_summary(
                        markdown, sq, smax, llm_client
                    )
                except Exception:
                    entry["summary"] = ""

        return entry

    tasks = [asyncio.create_task(_process_one(r)) for r in results]
    enriched = await asyncio.gather(*tasks)

    return list(enriched)


RICH_SEARCH_SYSTEM_PROMPT = """You are a search result enrichment engine. Your job is to take raw web search results
and produce improved output without inventing information.

Given a list of search results (each with url, title, and full page content), produce
enriched results by:

1. Writing a longer, more informative description for each result — 2-3 sentences that
   capture the key information from the page content relevant to the search query.

2. If an output_schema is provided, extract structured data from the page content
   matching the schema. Each extracted field must be grounded in the source content.
   Include a grounding field mapping each output field to the source URL.

Do NOT:
- Change URLs or titles
- Invent information not present in the page content
- Omit results — every result gets a description
- Return markdown formatting in descriptions (plain text only)

When system_prompt is provided, use it to guide your preferences (source selection,
recency, strictness)."""


DEEP_SEARCH_GAP_PROMPT = """You are a search coverage analyst. Given search results for a query,
identify what sub-topics, angles, or specific aspects are NOT covered.
Suggest 2-4 additional search queries that would fill these gaps.
Return ONLY a JSON array of query strings. No other text.

Example: ["alternative approach to X", "Y aspect of Z", "comparison between A and B"]"""


async def run_deep_search(
    query: str,
    limit: int,
    searxng_url: str = "http://searxng:8080",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
) -> dict:
    """Multi-pass search with LLM gap analysis and follow-up queries.

    Algorithm:
    1. Initial SearXNG search with primary query
    2. LLM evaluates result titles/descriptions for coverage gaps
    3. Formulates 2-4 follow-up queries targeting identified gaps
    4. Runs all follow-up queries in parallel via asyncio.gather
    5. Merges all results, URL-dedup (first occurrence wins)
    6. Returns SearchResult list + query_variations
    """
    import asyncio

    searxng = SearXNGClient(searxng_url)
    llm = LLMClient(llm_base_url, llm_api_key, llm_model)

    try:
        # 1. Initial search
        results, _health = await searxng.search(query, limit=max(limit, 5))

        if not results:
            return {"results": [], "query_variations": [query]}

        # 2. Gap analysis via LLM
        # Build context from result URLs, titles, descriptions (no scraping)
        result_summaries = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
            }
            for r in results[:limit]
        ]
        context = json.dumps(result_summaries)

        gap_prompt = (
            f'Search query: "{query}"\n\n'
            f"Current search results:\n{context}\n\n"
            f"What sub-topics, angles, or specific aspects are NOT covered by these results? "
            f"Suggest 2-4 additional search queries that would fill these gaps. "
            f"Return ONLY a JSON array of query strings. No other text."
        )

        follow_ups: list[str] = []
        try:
            gap_result = await llm.generate(
                system_prompt=DEEP_SEARCH_GAP_PROMPT,
                user_prompt=gap_prompt,
            )
            if gap_result and not gap_result.startswith("Error:"):
                # Parse the JSON array from the LLM response
                cleaned = gap_result.strip()
                cleaned = cleaned.removeprefix("```json")
                cleaned = cleaned.removeprefix("```")
                cleaned = cleaned.removesuffix("```")
                parsed = json.loads(cleaned)
                if isinstance(parsed, list):
                    follow_ups = [str(q) for q in parsed if q and str(q).strip()]
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("Deep search gap analysis failed: %s", e)

        # 3. Run all queries (original + follow-ups) in parallel
        all_queries = [query] + follow_ups

        async def search_one(q: str) -> list[dict]:
            r, _ = await searxng.search(q, limit=limit)
            return r

        all_result_lists = [results]
        if follow_ups:
            follow_up_results = await asyncio.gather(
                *[search_one(q) for q in follow_ups], return_exceptions=True
            )
            for fr in follow_up_results:
                if isinstance(fr, list):
                    all_result_lists.append(fr)
                elif isinstance(fr, Exception):
                    logger.warning("Follow-up search failed: %s", fr)

        # 4. Merge and URL-dedup (first occurrence wins)
        from .models import SearchResult

        seen_urls: set[str] = set()
        merged: list[SearchResult] = []
        for result_list in all_result_lists:
            for r in result_list:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    merged.append(
                        SearchResult(
                            url=url,
                            title=r.get("title", ""),
                            description=r.get("description", ""),
                        )
                    )

        logger.info(
            "Deep search: %d unique results from %d queries for: %s",
            len(merged),
            len(all_queries),
            query,
        )

        return {
            "results": merged[:limit],
            "query_variations": all_queries,
        }

    finally:
        await searxng.close()
        await llm.close()


async def run_rich_search(
    search_results: list[dict],
    query: str,
    limit: int = 5,
    output_schema: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    scraper_url: str = "http://scraper-svc:8001",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
) -> dict[str, Any] | None:
    """Enrich search results with scraped content and optional structured extraction.

    Returns dict with keys: content (structured data) and grounding (citations),
    or None if no results could be enriched.
    """
    import time

    start = time.monotonic()
    scraper = ScraperClient(scraper_url)
    llm = LLMClient(llm_base_url, llm_api_key, llm_model)

    try:
        top_results = search_results[:limit]

        # Scrape top results
        enriched = []
        for r in top_results:
            url = r.get("url", "")
            if not url:
                continue
            try:
                resp = await scraper.scrape(url)
                if resp.get("success") and resp.get("data", {}).get("markdown"):
                    content = resp["data"]["markdown"][:3000]  # Trim to 3K chars
                    enriched.append(
                        {
                            "url": url,
                            "title": r.get("title", ""),
                            "content": content,
                        }
                    )
                else:
                    enriched.append(
                        {
                            "url": url,
                            "title": r.get("title", ""),
                            "content": r.get("description", ""),
                        }
                    )
            except Exception:
                enriched.append(
                    {
                        "url": url,
                        "title": r.get("title", ""),
                        "content": r.get("description", ""),
                    }
                )

        if not enriched:
            return None

        # Build context for LLM
        context_parts = []
        for i, item in enumerate(enriched, start=1):
            context_parts.append(
                f"[{i}] URL: {item['url']}\nTitle: {item['title']}\nContent: {item['content']}"
            )

        context = "\n\n---\n\n".join(context_parts)

        # Build prompt
        prompt_parts = [
            f"Search query: {query}",
            f"\nSearch results with full content:\n\n{context}",
        ]

        if output_schema:
            schema_json = json.dumps(output_schema, indent=2)
            prompt_parts.append(
                f"\nExtract structured data matching this JSON Schema:\n```json\n{schema_json}\n```"
            )

        prompt = "\n".join(prompt_parts)

        # LLM call for enrichment or structured extraction
        effective_system = system_prompt or RICH_SEARCH_SYSTEM_PROMPT
        content = await llm.generate(
            system_prompt=effective_system,
            user_prompt=prompt,
        )

        result: dict[str, Any] = {}

        if output_schema:
            # Try to parse structured JSON from the response
            try:
                parsed = json.loads(content)
                result["content"] = parsed
            except json.JSONDecodeError:
                # Extract JSON block if wrapped in markdown
                if "```json" in content:
                    block = content.split("```json")[1].split("```")[0].strip()
                    try:
                        result["content"] = json.loads(block)
                    except json.JSONDecodeError:
                        result["content"] = content
                else:
                    result["content"] = content

            # Build grounding citations
            grounding = []
            for item in enriched:
                grounding.append(
                    {
                        "url": item["url"],
                        "title": item["title"],
                    }
                )
            result["grounding"] = grounding
        else:
            # Enrichment mode: parse the improved descriptions
            result["content"] = content
            result["grounding"] = [
                {"url": item["url"], "title": item["title"]} for item in enriched
            ]

        elapsed = int((time.monotonic() - start) * 1000)
        logger.info("Rich search completed in %dms for query: %s", elapsed, query)

        return result

    finally:
        await scraper.close()
        await llm.close()


# ── Find Similar ────────────────────────────────────────────────


async def run_find_similar(
    url: str,
    limit: int = 10,
    search_mode: str = "qdrant",
    scraper_url: str = "http://scraper-svc:8001",
    semantic_url: str = "http://semantic-svc:8003",
    searxng_url: str = "http://searxng:8080",
) -> list[dict]:
    """Find semantically similar pages for a given URL.

    Dispatches to the appropriate mode based on ``search_mode``.
    Returns a list of dicts with url, title, description.
    """

    if search_mode == "web":
        results = await _run_find_similar_web(
            url=url,
            limit=limit,
            scraper_url=scraper_url,
            semantic_url=semantic_url,
            searxng_url=searxng_url,
        )
    else:
        # Default to qdrant for any unrecognized mode
        results = await _run_find_similar_qdrant(
            url=url,
            limit=limit,
            scraper_url=scraper_url,
            semantic_url=semantic_url,
        )

    return results


async def _run_find_similar_qdrant(
    url: str,
    limit: int,
    scraper_url: str,
    semantic_url: str,
) -> list[dict]:
    """Find similar pages by scraping a URL, embedding its content,
    and searching the local Qdrant vector index."""
    from .semantic_client import SemanticClient

    scraper = ScraperClient(scraper_url)
    semantic = SemanticClient(semantic_url)

    try:
        # 1. Scrape the URL to get content
        scraped = await scraper.scrape(url)
        if not scraped.get("success"):
            return []
        markdown = scraped.get("data", {}).get("markdown", "")
        title = scraped.get("data", {}).get("metadata", {}).get("title", "")

        if not markdown.strip():
            return []

        # 2. Search Qdrant using the scraped content as the query
        # search_vector() embeds the text server-side and searches the index
        query_text = f"{title} {markdown[:3000]}"
        vector_results = await semantic.search_vector(query_text, limit=limit)

        return [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "description": r.get("content", "")[:200] if r.get("content") else "",
            }
            for r in vector_results
        ]
    finally:
        await scraper.close()
        await semantic.close()


async def _run_find_similar_web(
    url: str,
    limit: int,
    scraper_url: str,
    semantic_url: str,
    searxng_url: str,
) -> list[dict]:
    """Find similar pages by scraping a URL, extracting keywords,
    searching the open web, and reranking by cosine similarity."""
    import math

    from .semantic_client import SemanticClient

    scraper = ScraperClient(scraper_url)
    semantic = SemanticClient(semantic_url)
    searxng = SearXNGClient(searxng_url)

    try:
        # 1. Scrape the URL
        scraped = await scraper.scrape(url)
        if not scraped.get("success"):
            return []
        markdown = scraped.get("data", {}).get("markdown", "")
        title = scraped.get("data", {}).get("metadata", {}).get("title", "")

        if not markdown.strip():
            return []

        # 2. Extract key terms from content (title + first paragraph)
        first_para = markdown.split("\n\n")[0] if "\n\n" in markdown else markdown[:500]
        keywords = f"{title} {first_para}"

        # 3. Search the web with key terms (fetch extra for reranking headroom)
        results_list, _health = await searxng.search(keywords, limit=limit * 2)

        if not results_list:
            return []

        # 4. Embed the query URL's content for reranking
        query_embeddings = await semantic.embed([markdown[:5000]])
        query_embedding = query_embeddings[0]

        # 5. Embed each candidate result's description
        candidates = [
            {
                "url": r.get("url", ""),
                "title": r.get("title", ""),
                "description": r.get("description", ""),
            }
            for r in results_list[: limit * 2]
        ]
        texts_to_embed = [f"{c['title']} {c['description']}" for c in candidates]
        if not texts_to_embed:
            return []

        candidate_embeddings = await semantic.embed(texts_to_embed)

        # 6. Rank by cosine similarity
        scored = []
        for i, (candidate, emb) in enumerate(zip(candidates, candidate_embeddings)):
            dot = sum(a * b for a, b in zip(query_embedding, emb))
            norm_q = math.sqrt(sum(a * a for a in query_embedding))
            norm_c = math.sqrt(sum(b * b for b in emb))
            sim = dot / (norm_q * norm_c) if norm_q > 0 and norm_c > 0 else 0.0
            scored.append((sim, candidate))

        scored.sort(key=lambda x: x[0], reverse=True)

        # 7. Return top N
        return [
            {
                "url": c["url"],
                "title": c["title"],
                "description": c["description"],
            }
            for _, c in scored[:limit]
        ]
    finally:
        await scraper.close()
        await semantic.close()
        await searxng.close()


async def run_search_stream(
    query: str,
    limit: int = 5,
    search_type: str = "fast",
    retrieval_mode: str = "keyword",
    categories: list[str] | None = None,
    sources: list[str] | None = None,
    output_schema: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    searxng_url: str = "http://searxng:8080",
    scraper_url: str = "http://scraper-svc:8001",
    semantic_url: str = "http://semantic-svc:8003",
    llm_base_url: str = "https://api.openai.com/v1",
    llm_api_key: str = "",
    llm_model: str = "gpt-4o-mini",
    max_searches_per_request: int = 5,
):
    """Streaming version of /v2/search. Yields SSE-suitable dicts.

    Phase 1: Search via SearXNG (or Qdrant for vector modes).
    Phase 2 (rich only): Scrape top results + LLM synthesis.

    Yields:
      {"type": "search_result", "result": {"url":"...","title":"...","description":"..."}}
      {"type": "scrape_result", "url": "...", "contents": {"markdown": "..."}}  (rich only)
      {"type": "token", "content": "..."}  (rich only, no output_schema)
      {"type": "done", "total_results": N, "latency_ms": N, "output": ...}
      {"type": "error", "content": "..."}
    """
    import time

    start = time.monotonic()

    searxng = SearXNGClient(searxng_url, max_searches=max_searches_per_request)
    scraper = ScraperClient(scraper_url)
    llm = LLMClient(llm_base_url, llm_api_key, llm_model)

    try:
        # ── Phase 1: Search ──────────────────────────────────────
        if retrieval_mode == "vector":
            from .semantic_client import SemanticClient

            semantic = SemanticClient(semantic_url)
            try:
                vector_results = await semantic.search_vector(query, limit=limit)
                search_results = [
                    {"url": r["url"], "title": r["title"], "description": ""}
                    for r in vector_results
                ]
            finally:
                await semantic.close()

        elif retrieval_mode == "hybrid_vector":
            from .semantic_client import SemanticClient

            semantic = SemanticClient(semantic_url)
            try:
                searxng_results, _health = await searxng.search(
                    query, limit=limit, categories=categories, sources=sources
                )
                vector_results = await semantic.search_vector(query, limit=limit)

                seen: set[str] = set()
                merged: list[dict] = []
                for r in searxng_results:
                    if r["url"] not in seen:
                        seen.add(r["url"])
                        merged.append(r)
                for r in vector_results:
                    if r["url"] not in seen:
                        seen.add(r["url"])
                        merged.append(
                            {"url": r["url"], "title": r["title"], "description": ""}
                        )
                search_results = merged[:limit]
            finally:
                await semantic.close()

        else:
            # Keyword, semantic, hybrid: standard SearXNG path
            results, _health = await searxng.search(
                query, limit=limit, categories=categories, sources=sources
            )
            search_results = results

        # ── Yield search_result events ──────────────────────────
        for r in search_results:
            yield {
                "type": "search_result",
                "result": {
                    "url": r.get("url", ""),
                    "title": r.get("title", ""),
                    "description": r.get("description", ""),
                },
            }

        total_results = len(search_results)

        # Semantic/hybrid reranking for keyword results
        if retrieval_mode in ("semantic", "hybrid") and search_results:
            from .semantic_client import SemanticClient

            semantic = SemanticClient(semantic_url)
            scraper_rerank = ScraperClient(scraper_url)
            try:
                urls_to_scrape = [r["url"] for r in search_results[:limit]]
                contents = []
                for url in urls_to_scrape:
                    try:
                        scraped = await scraper_rerank.scrape(url)
                        content = (
                            scraped.get("data", {}).get("markdown", "")
                            if scraped.get("success")
                            else ""
                        )
                        contents.append(content[:2000])
                    except Exception:
                        contents.append("")

                if retrieval_mode == "hybrid":
                    reranked = await semantic.rerank(
                        query,
                        [r.get("description", "") for r in search_results[:limit]],
                        top_k=limit,
                    )
                    new_order = [item["index"] for item in reranked]
                    search_results = [
                        search_results[i] for i in new_order if i < len(search_results)
                    ]
                else:
                    embeddings = await semantic.embed([query] + contents)
                    query_em = embeddings[0]
                    similarities = [
                        sum(a * b for a, b in zip(query_em, doc_em))
                        for doc_em in embeddings[1:]
                    ]
                    ranked_indices = sorted(
                        range(len(similarities)),
                        key=lambda i: similarities[i],
                        reverse=True,
                    )
                    search_results = [
                        search_results[i]
                        for i in ranked_indices
                        if i < len(search_results)
                    ]
            finally:
                await semantic.close()
                await scraper_rerank.close()

        # ── Phase 2: Rich enrichment (scrape + LLM) ─────────────
        output = None
        if search_type == "rich" and search_results:
            top_results = search_results[:limit]

            # Scrape each result, yield scrape_result events
            enriched: list[dict] = []
            import asyncio

            semaphore = asyncio.Semaphore(3)
            queue: asyncio.Queue = asyncio.Queue()

            async def _scrape_and_queue(item: dict) -> None:
                url = item.get("url", "")
                if not url:
                    await queue.put(None)
                    return
                async with semaphore:
                    try:
                        resp = await asyncio.wait_for(scraper.scrape(url), timeout=20)
                        if resp.get("success") and resp.get("data", {}).get("markdown"):
                            md = resp["data"]["markdown"][:3000]
                            await queue.put(
                                {
                                    "scrape_event": {
                                        "type": "scrape_result",
                                        "url": url,
                                        "contents": {"markdown": md},
                                    },
                                    "enriched": {
                                        "url": url,
                                        "title": item.get("title", ""),
                                        "content": md,
                                    },
                                }
                            )
                            return
                    except Exception:
                        pass
                    await queue.put(
                        {
                            "enriched": {
                                "url": url,
                                "title": item.get("title", ""),
                                "content": item.get("description", ""),
                            },
                        }
                    )

            tasks = [asyncio.create_task(_scrape_and_queue(r)) for r in top_results]

            completed_count = 0
            while completed_count < len(tasks):
                item = await queue.get()
                completed_count += 1
                if item is None:
                    continue
                if "scrape_event" in item:
                    yield item["scrape_event"]
                enriched.append(item["enriched"])

            # Wait for any remaining tasks to settle
            await asyncio.gather(*tasks, return_exceptions=True)

            if enriched:
                # Build context for LLM
                context_parts = []
                for i, item in enumerate(enriched, start=1):
                    context_parts.append(
                        f"[{i}] URL: {item['url']}\nTitle: {item['title']}\n"
                        f"Content: {item['content']}"
                    )
                context = "\n\n---\n\n".join(context_parts)

                if output_schema:
                    # Structured extraction: single LLM call
                    prompt_parts = [
                        f"Search query: {query}",
                        f"\nSearch results with full content:\n\n{context}",
                        f"\nExtract structured data matching this JSON Schema:\n"
                        f"```json\n{json.dumps(output_schema, indent=2)}\n```",
                    ]
                    prompt = "\n".join(prompt_parts)
                    effective_system = system_prompt or RICH_SEARCH_SYSTEM_PROMPT
                    content = await llm.generate(
                        system_prompt=effective_system,
                        user_prompt=prompt,
                    )

                    parsed_output: Any = content
                    try:
                        parsed_output = json.loads(content)
                    except json.JSONDecodeError:
                        if "```json" in content:
                            block = content.split("```json")[1].split("```")[0].strip()
                            try:
                                parsed_output = json.loads(block)
                            except json.JSONDecodeError:
                                pass

                    output = {
                        "content": parsed_output,
                        "grounding": [
                            {"url": item["url"], "title": item["title"]}
                            for item in enriched
                        ],
                    }
                else:
                    # Enrichment mode: stream LLM tokens
                    prompt_parts = [
                        f"Search query: {query}",
                        f"\nSearch results with full content:\n\n{context}",
                    ]
                    prompt = "\n".join(prompt_parts)
                    effective_system = system_prompt or RICH_SEARCH_SYSTEM_PROMPT

                    full_result = ""
                    async for event in llm.generate_stream(
                        system_prompt=effective_system,
                        user_prompt=prompt,
                    ):
                        if event["type"] == "token":
                            full_result += event["content"]
                            yield {"type": "token", "content": event["content"]}
                        elif event["type"] == "error":
                            yield {"type": "error", "content": event["content"]}
                            break
                        elif event["type"] == "done":
                            full_result = event["full_content"]

                    output = {
                        "content": full_result,
                        "grounding": [
                            {"url": item["url"], "title": item["title"]}
                            for item in enriched
                        ],
                    }

        elapsed = int((time.monotonic() - start) * 1000)
        done_event: dict[str, Any] = {
            "type": "done",
            "total_results": total_results,
            "latency_ms": elapsed,
        }
        if output is not None:
            done_event["output"] = output
        yield done_event

    finally:
        await searxng.close()
        await scraper.close()
        await llm.close()
