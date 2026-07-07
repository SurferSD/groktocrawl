# Search Architecture with Vertical Categories

* Status: superseded by ADR-0043
* Deciders: magnus, jasper
* Date: 2026-06-05

Technical Story: The initial `/v2/search` implementation sequentially scraped every search result page, producing 50x slowdowns and returning results incompatible with the Firecrawl v2 API contract.

## Context and Problem Statement

Search went through three distinct evolutions:

1. **v0 (initial):** Scraped each result page via Playwright — produced 50x slowdowns. Results returned as flat list, not grouped by source type.
2. **v1 (fix):** Removed per-result scraping. Results returned grouped by source type (`data.web`), matching Firecrawl v2 spec.
3. **v2 (enhance):** Added vertical search categories — web, news, social — each with separate endpoints, backed by SearXNG engine filtering.

The architecture needed to stabilize around a format that works for both Firecrawl SDK compatibility and category-aware clients.

## Decision Drivers

* Backward compatibility with Firecrawl SDK v4 (web-based response shape)
* Ability to add vertical categories without breaking existing callers
* Self-hosted search via SearXNG — no external API dependency
* Results must be grouped by source type, not flat-listed

## Considered Options

* **A. Multi-endpoint with categories** — `/v2/search` (default web), `/v2/search/news`, `/v2/search/social` with query parameter override.
* **B. Single endpoint with category filter** — One endpoint, `?category=news` parameter.
* **C. Firecrawl-compatible only** — No categories, flat results only.

## Decision Outcome

Chosen option: **A. Multi-endpoint with category filter**. `/v2/search` defaults to web results. Categories filter via query parameter. Response format groups by source type:
```json
{
  "data": {
    "web": [{"url": "...", "title": "...", "description": "..."}]
  }
}
```

### Positive Consequences

* Firecrawl SDK compatibility — existing clients work unchanged
* Category-aware clients can request specific engines
* SearXNG backend handles engine routing without custom code

### Negative Consequences

* Response format differs from Firecrawl v1 (`data.web` vs flat `data`). Mitigated by adding `/v1/search` alias for backward compat.
* Category filtering depends on SearXNG engine availability

## Links

* Implemented by PR #84 (categories), #83 (Firecrawl alignment), #69 (50x speedup), #66 (grouped results)
* Defined by `agent-svc/agent/searxng_client.py`
