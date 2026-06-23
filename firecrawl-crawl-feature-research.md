# Firecrawl Crawl API — Comprehensive Feature Research

Generated 2026-06-19 from Firecrawl docs, blog guides, API reference, and glossary.

Sources consulted:
- https://docs.firecrawl.dev/api-reference/endpoint/crawl-post
- https://docs.firecrawl.dev/api-reference/endpoint/crawl-get
- https://docs.firecrawl.dev/api-reference/endpoint/crawl-delete
- https://docs.firecrawl.dev/api-reference/endpoint/crawl-get-errors
- https://docs.firecrawl.dev/api-reference/endpoint/crawl-active
- https://docs.firecrawl.dev/api-reference/endpoint/crawl-params-preview
- https://docs.firecrawl.dev/api-reference/endpoint/scrape (for scrapeOptions reference)
- https://docs.firecrawl.dev/api-reference/endpoint/webhook-crawl-started
- https://docs.firecrawl.dev/api-reference/endpoint/webhook-crawl-page
- https://docs.firecrawl.dev/api-reference/endpoint/webhook-crawl-completed
- https://www.firecrawl.dev/blog/mastering-the-crawl-endpoint-in-firecrawl
- https://www.firecrawl.dev/glossary/web-crawling-apis/deduplicate-crawl-pages-rag

---

## 1. INITIATION / CONFIGURATION

### `url` (string, required)
The base URL to start crawling from.
**GroktoCrawl:** ✅ HAS — `CrawlRequest.url`

### `prompt` (string)
Natural language description of what to crawl. Firecrawl translates this into the crawl parameters (includePaths, excludePaths, maxDiscoveryDepth, etc.). Explicitly-set parameters override the NL-derived equivalents.
**GroktoCrawl:** ❌ MISSING — No NL→param translation for crawl.

### `webhook` (object)
A webhook specification object with `url`, `events`, and optional `metadata`.
Firecrawl sends three event types for crawls:
- `crawl.started` — job started
- `crawl.page` — each page scraped (with the page data)
- `crawl.completed` — job finished (empty data; results via GET)

All webhooks include `X-Firecrawl-Signature` header (HMAC-SHA256).
**GroktoCrawl:** ⚠️ PARTIAL — Has `webhook` field in CrawlRequest and `deliver_webhook()` function with HMAC signing, but only fires on "completed"/"failed". No per-page webhooks, no "started" event. No `events` filter or `metadata` echo.

### POST `/v2/crawl/params-preview`
NL prompt → params preview endpoint. Takes `url` + `prompt`, returns derived parameters (includePaths, excludePaths, maxDepth, crawlEntireDomain, allowExternalLinks, allowSubdomains, ignoreRobotsTxt, robotsUserAgent, deduplicateSimilarURLs, delay, limit, etc.).
**GroktoCrawl:** ❌ MISSING — No params-preview endpoint.

---

## 2. DISCOVERY & SCOPE

### `sitemap` (enum: "include" | "skip" | "only", default: "include")
- `"include"` — use sitemap alongside HTML link discovery
- `"skip"` — ignore sitemap, only follow HTML links
- `"only"` — only crawl URLs from sitemap (+ start URL), no HTML link discovery
**GroktoCrawl:** ❌ MISSING — `CrawlRequest` has `ignore_sitemap: bool` which only covers the binary skip/include case but isn't actually implemented in the worker. No "only" mode.

### `maxDiscoveryDepth` (integer)
Maximum depth from root. Root page = depth 0, its direct links = depth 1, etc. Pages at max depth are still scraped but their links are not followed. Sitemap-discovered pages count as depth 0.
**GroktoCrawl:** ⚠️ PARTIAL — `CrawlRequest.max_depth` field exists but worker doesn't actually do any link discovery at all (only scrapes the starting URL).

### `limit` (integer, default: 10000)
Maximum number of pages to crawl. Firecrawl pre-checks credits against limit before starting; returns 402 if insufficient.
**GroktoCrawl:** ⚠️ PARTIAL — `CrawlRequest.max_pages` (default: 10) but worker ignores it (single page only).

### `includePaths` (string[])
URL pathname regex patterns to include in the crawl. Only matching paths are included. The starting URL is also checked — if it doesn't match, crawl may return 0 pages.
**GroktoCrawl:** ✅ HAS (in model) — Not yet implemented in worker.

### `excludePaths` (string[])
URL pathname regex patterns that exclude matching URLs from the crawl.
**GroktoCrawl:** ✅ HAS (in model) — Not yet implemented in worker.

### `regexOnFullURL` (boolean, default: false)
When true, includePaths/excludePaths regex patterns match against the full URL (including query parameters), not just pathname.
**GroktoCrawl:** ❌ MISSING

### `crawlEntireDomain` (boolean, default: false)
- `false`: Only crawls deeper (child) URLs. `/features/x` → `/features/x/tips` ✅ but won't follow `/pricing` or `/`.
- `true`: Follows any internal links including siblings and parents.
**GroktoCrawl:** ❌ MISSING

### `allowExternalLinks` (boolean, default: false)
Allow following links to external domains.
**GroktoCrawl:** ❌ MISSING

### `allowSubdomains` (boolean, default: false)
Allow following links to subdomains of the main domain.
**GroktoCrawl:** ❌ MISSING

### `ignoreQueryParameters` (boolean, default: false)
Do not re-scrape the same path with different query parameters. This is URL-level deduplication: `?ref=abc` and `?ref=xyz` collapse to one fetch.
**GroktoCrawl:** ❌ MISSING

### Deduplication Strategy (automatic, not a parameter)
Per Firecrawl glossary, they do multi-layer dedup:
1. **URL normalization** (pre-fetch): trailing slashes, tracking params, protocol variants
2. **Canonical tag check** (post-fetch): `<link rel="canonical">`
3. **Exact content hash** (post-fetch): on extracted text, not raw HTML
4. **Near-duplicate filtering** (optional): embedding similarity for syndicated/template-heavy content

Firecrawl handles URL-level dedup automatically within a crawl run. `ignoreQueryParameters` reduces the volume needing downstream content dedup.
**GroktoCrawl:** ❌ MISSING — No dedup at any layer (only fetches one URL).

---

## 3. RATE LIMITING & CONCURRENCY

### `delay` (number, seconds)
Delay between scrapes. Setting this forces `maxConcurrency` to 1. Different from `waitFor` (which is per-page render time in ms).
**GroktoCrawl:** ❌ MISSING

### `maxConcurrency` (integer)
Maximum number of concurrent scrapes for this crawl. If not specified, uses team's account-level concurrency limit.
**GroktoCrawl:** ❌ MISSING

---

## 4. robots.txt

### `ignoreRobotsTxt` (boolean, default: false)
Ignore website's robots.txt rules. **Enterprise only.**
**GroktoCrawl:** ❌ MISSING

### `robotsUserAgent` (string)
Custom User-Agent for robots.txt evaluation. **Enterprise only.**
**GroktoCrawl:** ❌ MISSING

Note: The errors endpoint (`GET /v2/crawl/{id}/errors`) returns a `robotsBlocked` list of URLs skipped due to robots.txt.

---

## 5. SCRAPE OPTIONS (via `scrapeOptions` object)

Firecrawl's crawl `scrapeOptions` mirrors the `/v2/scrape` endpoint options exactly. They apply to every page in the crawl.

### Formats
Array of format strings or format objects:
- `"markdown"` — clean markdown (default)
- `"html"` — cleaned HTML
- `"rawHtml"` — raw HTML
- `"links"` — array of links found on page
- `"images"` — images metadata
- `"screenshot"` — temporary PNG URL (expires 24h)
- `"json"` — structured extraction with LLM (takes `{ type: "json", schema: {...} }`)
- `"summary"` — LLM summary
- `"changeTracking"` — compares against previous scrape
- `"branding"` — logo, colors, fonts, typography, spacing, components
- `"product"` — title, variants, price, availability, images
- `"audio"` — audio extraction
- `"video"` — video extraction
- `"question"` — ask a question about the page
- `"highlights"` — content highlights

**GroktoCrawl:** ❌ MISSING — No `scrapeOptions` passthrough in CrawlRequest model at all. The worker hardcodes `scraper.scrape(url)` with no format control.

### `onlyMainContent` (boolean, default: true)
Return only main page content, excluding headers/navs/footers. Deterministic HTML-level filter, no LLM.
**GroktoCrawl:** ❌ MISSING

### `onlyCleanContent` (boolean, default: false)
**Beta.** LLM-based pass over generated markdown to remove residual boilerplate that `onlyMainContent` can miss (cookie banners, ads, social widgets, breadcrumbs, newsletter signups, comments, related-article lists). Preserves headings, lists, tables, code blocks, images, inline links. Skipped when markdown exceeds the cleaning model's token limit. Not supported on zero-data-retention requests.
**GroktoCrawl:** ❌ MISSING

### `includeTags` (string[])
Tags/classes/IDs to include in output.
**GroktoCrawl:** ❌ MISSING

### `excludeTags` (string[])
Tags/classes/IDs to exclude from output.
**GroktoCrawl:** ❌ MISSING

### `maxAge` (integer, default: 172800000 ms = 2 days)
Cache control: return cached version if younger than this. Can speed up scrapes by ~500%.
**GroktoCrawl:** ❌ MISSING — No caching layer.

### `minAge` (integer)
Cache-only mode: only checks cache, never triggers fresh scrape. Minimum age of cached data. If no match, returns 404 `SCRAPE_NO_CACHED_DATA`. Set to 1 to accept any cached data.
**GroktoCrawl:** ❌ MISSING

### `headers` (object)
Custom headers sent with the scrape request (cookies, user-agent, etc.).
**GroktoCrawl:** ❌ MISSING

### `waitFor` (integer, default: 0)
Milliseconds to wait before fetching content, in addition to Firecrawl's smart wait.
**GroktoCrawl:** ❌ MISSING

### `mobile` (boolean, default: false)
Emulate mobile device scraping. Useful for responsive pages and mobile screenshots.
**GroktoCrawl:** ❌ MISSING

### `skipTlsVerification` (boolean, default: true)
Skip TLS certificate verification.
**GroktoCrawl:** ❌ MISSING (scraper-svc may have this internally, but not configurable from crawl API)

### `timeout` (integer, default: 60000 ms, min: 1000, max: 300000)
Per-page request timeout.
**GroktoCrawl:** ❌ MISSING (per-page timeout not configurable)

### `parsers` (object[])
Controls file processing. When `"pdf"` is included (default), PDFs are extracted to markdown (1 credit per page). Empty array returns PDF as base64 (flat 1 credit).
**GroktoCrawl:** ❌ MISSING

### `actions` (object[])
Browser actions to perform before grabbing content:
- `wait` (by duration ms)
- `waitFor` (wait for element selector)
- `screenshot` (full page or element)
- `click` (by selector)
- `write` (type text)
- `press` (press a key)
- `scroll` (scroll to element or by pixels)
- `scrape` (scrape current state)
- `executeJavascript` (run JS)
- `generatePDF` (generate PDF of current state)

**GroktoCrawl:** ❌ MISSING — Browser actions exist on the separate `/v2/browser` endpoint but not as scrape options.

### `location` (object: { country, languages })
Location settings for proxy selection and language/timezone emulation. Defaults to `"US"`.
**GroktoCrawl:** ❌ MISSING

### `removeBase64Images` (boolean, default: true)
Remove base64-encoded images from markdown output. Alt text preserved with placeholder.
**GroktoCrawl:** ❌ MISSING

### `blockAds` (boolean, default: true)
Enable ad blocking and cookie popup blocking.
**GroktoCrawl:** ❌ MISSING

### `proxy` (enum: "basic" | "enhanced" | "auto", default: "auto")
- `basic`: Fast, for sites with minimal anti-bot
- `enhanced`: For advanced anti-bot sites. Costs up to 5 credits/request.
- `auto`: Tries basic first, falls back to enhanced on failure. Only bills enhanced credits if fallback used.
**GroktoCrawl:** ❌ MISSING

### `storeInCache` (boolean, default: true)
Whether to store the page in Firecrawl's cache. Set to false for data protection concerns. Some params (actions, headers) force this to false.
**GroktoCrawl:** ❌ MISSING

### `lockdown` (boolean, default: false)
Cache-only mode: never makes outbound request. On cache miss, returns 404 `SCRAPE_LOCKDOWN_CACHE_MISS`. URL is never logged on miss. Treated as zero data retention. Default `maxAge` extended to 2 years. Billed at 5 credits on hit, 1 on miss. Designed for compliance/air-gapped environments.
**GroktoCrawl:** ❌ MISSING

### `redactPII` (boolean | object, default: false)
Redact PII from returned markdown. Pass `true` for defaults, or an object to tune mode, entities, and replacement style.
**GroktoCrawl:** ❌ MISSING

### `profile` (object: { name, saveChanges })
Enable persistent browser storage across scrape and interact sessions. Sessions with the same profile name share browser state (cookies, localStorage, session data).
**GroktoCrawl:** ❌ MISSING

---

## 6. RESULTS & STATUS

### `POST /v2/crawl` — Create
Response: `{ success: true, id: string, url: string }`
**GroktoCrawl:** ✅ HAS — `CrawlCreateResponse { success, id }`. Missing the `url` field in response.

### `GET /v2/crawl/{id}` — Status
Response fields:
- `status` (string): "scraping" | "completed" | "failed"
- `total` (int): total pages attempted
- `completed` (int): pages successfully scraped
- `creditsUsed` (int): credits consumed
- `expiresAt` (datetime): when results expire
- `createdAt` (datetime): when job started
- `completedAt` (datetime): when job finished (terminal states only)
- `duration` (number): elapsed seconds
- `next` (string | null): URL for next 10MB chunk if response > 10MB
- `data` (array): scraped page documents, each with markdown, html, rawHtml, links, screenshot, metadata

**GroktoCrawl:** ⚠️ PARTIAL — `CrawlStatusResponse` has `status`, `completed`, `total`, `credits_used`, `data`, `error`. Missing: `expiresAt`, `createdAt`, `completedAt`, `duration`, `next` (pagination), full per-page metadata shape.

### `DELETE /v2/crawl/{id}` — Cancel
Response: `{ status: "cancelled" }`
**GroktoCrawl:** ✅ HAS — Returns `{ success: true }` via `AgentCancelResponse`. Slightly different response shape but functionally equivalent.

### `GET /v2/crawl/{id}/errors` — Errors
Response:
- `errors[]`: array of `{ id, timestamp, url, error }`
- `robotsBlocked[]`: array of URL strings blocked by robots.txt

**GroktoCrawl:** ❌ MISSING

### `GET /v2/crawl/active` — Active crawls
Lists all active crawls for the authenticated team with full options.
**GroktoCrawl:** ⚠️ PARTIAL — Has `GET /v2/activity` which lists all active jobs across all types. No crawl-specific filtering, and no per-job options display.

### Webhook Events (3 for crawl)
- `crawl.started` — `{ type, id, webhookId, data: [], metadata: {} }`
- `crawl.page` — `{ success, type, id, webhookId, data: [pageDoc], error: string | null, metadata: {} }` — fires per page as scraped
- `crawl.completed` — `{ success, type, id, webhookId, data: [], metadata: {} }`

All include HMAC signature via `X-Firecrawl-Signature` header (uses specific webhook secret, not the API token).
**GroktoCrawl:** ❌ MISSING — Only fires one webhook on completion/failure. No per-page events, no started event, no webhookId for dedup, no metadata echo.

### SDK Delivery Modes
- `crawl()` — synchronous, blocks until done
- `start_crawl()` + `get_crawl_status()` — async polling
- `AsyncFirecrawl.watcher()` — WebSocket streaming (with HTTP polling fallback)
- Webhooks — HTTP POST push

**GroktoCrawl:** ⚠️ PARTIAL — Has sync create + poll (store-based). No WebSocket streaming. No SDK-level watcher.

---

## 7. ADVANCED / SECURITY

### `zeroDataRetention` (boolean, default: false)
If true, enables zero data retention for this crawl. Must contact Firecrawl to enable.
**GroktoCrawl:** ❌ MISSING

### Prompt-to-Params Translation (NL → Crawl Config)
The `prompt` field in crawl POST is translated via LLM into crawl parameters. The `/v2/crawl/params-preview` endpoint exposes this translation. Different prompts produce different includePaths, maxDepth, etc.
**GroktoCrawl:** ❌ MISSING

### Credit Pre-check
Before starting a crawl, Firecrawl checks that your remaining credits can cover the `limit`. If not, returns `402 Payment Required`.
**GroktoCrawl:** ❌ MISSING — No credit system.

### `deduplicateSimilarURLs` (in params-preview output)
A parameter surfaced in params-preview that controls URL dedup similarity. Not directly documented as a crawl POST parameter but appears in the preview output.
**GroktoCrawl:** ❌ MISSING

---

## 8. ENDPOINT SUMMARY (Full Firecrawl Crawl Surface)

| Method | Path | GroktoCrawl Status |
|--------|------|-------------------|
| POST | `/v2/crawl` | ✅ EXISTS (minimal impl) |
| GET | `/v2/crawl/{id}` | ✅ EXISTS (partial fields) |
| DELETE | `/v2/crawl/{id}` | ✅ EXISTS |
| GET | `/v2/crawl/{id}/errors` | ❌ MISSING |
| GET | `/v2/crawl/active` | ⚠️ PARTIAL (generic activity endpoint) |
| POST | `/v2/crawl/params-preview` | ❌ MISSING |

## 9. GROKTOCRAWL CRITICAL GAPS SUMMARY

The current GroktoCrawl crawl implementation is fundamentally a **single-page scrape with a crawl-shaped API shell**:

1. **No link discovery** — the worker only scrapes the starting URL. `max_pages`, `max_depth`, `include_paths`, `exclude_paths`, `ignore_sitemap` fields exist in the model but are never used in processing.

2. **No scrapeOptions passthrough** — all format control, content filtering, JS rendering, proxy selection, caching, PII redaction, ad blocking, and browser actions are missing.

3. **No concurrency** — no `delay`, `maxConcurrency`, or parallel scraping infrastructure.

4. **No deduplication** — no URL normalization, canonical checking, content hashing, or near-duplicate filtering.

5. **Missing endpoints** — `/errors`, `/params-preview`, crawl-specific `/active`.

6. **Minimal webhooks** — only completion/failure, no per-page events, no started event, no webhookId for dedup.

7. **No caching** — no `maxAge`/`minAge`/`storeInCache`/`lockdown` infrastructure.

8. **No NL→params translation** — no `prompt` field or params-preview.

9. **Response shape gaps** — missing `expiresAt`, `createdAt`, `completedAt`, `duration`, `next` (pagination), full per-page metadata.

10. **Missing Firecrawl API fields** — `crawlEntireDomain`, `allowExternalLinks`, `allowSubdomains`, `ignoreQueryParameters`, `regexOnFullURL`, `sitemap` modes (enum not bool), `scrapeOptions`, `zeroDataRetention`, full webhook shape.

## 10. PRIORITY IMPLEMENTATION ORDER (RECOMMENDATION)

### Tier 1 — Core crawl functionality (MVP)
1. Link discovery engine (HTML link extraction, sitemap parsing)
2. Depth-first / breadth-first traversal with `maxDepth` / `maxDiscoveryDepth`
3. `limit` enforcement (stop at `maxPages`)
4. `includePaths` / `excludePaths` filtering with regex
5. URL-level dedup within a crawl run
6. `ignoreQueryParameters` for query-string collapse

### Tier 2 — Scope and discovery
7. `sitemap` modes (include/skip/only) with proper sitemap parsing
8. `crawlEntireDomain` (sibling/parent URL following)
9. `allowSubdomains`
10. `allowExternalLinks` (with safety guard)
11. `regexOnFullURL`

### Tier 3 — Scrape options passthrough
12. `scrapeOptions` model with all format/content/rendering fields
13. Pass scrapeOptions through to scraper-svc
14. `onlyMainContent`, `includeTags`, `excludeTags`
15. `waitFor`, `timeout`, `mobile`, `headers`

### Tier 4 — Concurrency and rate limiting
16. `maxConcurrency` with asyncio semaphore or task pool
17. `delay` (between-request pacing)
18. Parallel scraping infrastructure

### Tier 5 — Caching and advanced features
19. Response caching layer (`maxAge`/`minAge`)
20. Content dedup (canonical check, content hash)
21. `robots.txt` parsing and enforcement
22. `GET /v2/crawl/{id}/errors` endpoint
23. `GET /v2/crawl/active` (or enhance existing activity)

### Tier 6 — Full parity
24. NL→params translation (`prompt` field + `/v2/crawl/params-preview`)
25. Per-page webhooks (`crawl.page` events)
26. WebSocket streaming (`watcher()` equivalent)
27. `lockdown`, `redactPII`, `profile`, `zeroDataRetention`
28. Advanced scrapeOptions (`actions`, `location`, `proxy`, `blockAds`, `parsers`, `removeBase64Images`)
29. Response shape parity (all fields: `expiresAt`, `createdAt`, `completedAt`, `duration`, `next`, full metadata, per-page `links`/`screenshot`/`html`/`rawHtml`)
30. Credit system with pre-check
