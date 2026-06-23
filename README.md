# GroktoCrawl

**Self-hosted Firecrawl alternative with semantic search, grounded Q&A, site adapters, and an autonomous research agent. MIT licensed. One `docker compose up` and you're running.**

GroktoCrawl implements the Firecrawl v2 API surface — scrape, search, map, crawl, extract, browser sessions, and monitors — plus capabilities Firecrawl doesn't offer: a persistent **semantic search engine** with Qdrant vector index, a **grounded Q&A endpoint** with citations, a **web portal** for human users, site **adapters for GitHub/Substack/Reddit/YouTube/Bluesky/Gutenberg/Greenhouse/AshbyHQ/Shopify**, an **intelligent scrape cache** with ETag/Last-Modified revalidation, and a full **observability stack** with health probes and Prometheus metrics. Runs entirely in Docker on your own hardware. Bring your own LLM or use the built-in fixtures.

## Quick Start

```bash
cp .env.sample .env
docker compose --profile fixture up --build -d
```

Eight containers start. Add `--profile fixture` to also start a built-in LLM fixture and test site — useful for evaluation without an API key. Omit it for production (configure a real LLM in `.env` instead).

The stack includes a SearXNG-compatible search service, a smart scraper, and an Ofelia-scheduled monitor system.

```bash
# CLI
./groktocrawl scrape https://example.com
./groktocrawl search "raspberry pi 5" --limit 3
./groktocrawl agent "What were the key Google I/O 2025 announcements?"

# Or raw curl
curl http://localhost:8080/health
curl -X POST http://localhost:8080/v2/scrape -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

## Production Setup

Edit `.env` to point at a real LLM:

```env
# DeepSeek
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-v4-flash

# OpenAI
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini

# Ollama (local)
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=llama3.2
```

## Architecture

```mermaid
flowchart TD
    subgraph compose["docker-compose.yml"]
        valkey[("valkey<br/>(queue + storage)")]
        qdrant[("qdrant<br/>(vector index)")]
        searxng["slopsearx<br/>(web search)"]
        scraper("scraper-svc<br/>(smart fetch)")
        browser["browser-svc<br/>(Playwright sessions)"]
        semantic["semantic-svc<br/>(embeddings + near-dup)"]
        agent("agent-svc<br/>(FastAPI + workers)")
        portal["portal-svc<br/>(web UI)"]
        ofelia["ofelia<br/>(cron scheduler)"]

        valkey --- agent
        qdrant --- semantic
        searxng --- agent
        scraper --- agent
        semantic --- agent
        browser --- agent
        portal --> agent
        ofelia -.->|docker exec| agent
    end
    llm_provider("LLM Provider<br/>(DeepSeek / OpenAI / Ollama / fixture)")
    llm_provider -.->|LLM_BASE_URL| agent

    style valkey fill:#ffe0b0
    style qdrant fill:#ffe0b0
    style searxng fill:#b0d4ff
    style scraper fill:#b0ffb0
    style browser fill:#d4b0ff
    style semantic fill:#ffd4b0
    style agent fill:#ffb0b0
    style portal fill:#b0d0ff
    style ofelia fill:#b0b0b0
```

The scraper uses a **three-tier strategy**: check `/llms.txt` first, try `Accept: text/markdown` second, render with Playwright third.

Every scrape response includes a **`quality` field** with post-extraction content quality assessment (boilerplate detection, completeness checks, block page detection). See `docs/adr/0016-extraction-quality-gates.md` for details.

## CLI

`groktocrawl` is a CLI tool in the repo root. It needs `requests`.

If you want to avoid installing dependencies into your global Python, use a repo-local `uv` environment:

```bash
uv venv
uv pip install requests
uv run ./groktocrawl scrape https://example.com
```

To expose a global `groktocrawl` command while keeping dependencies isolated, create a small wrapper somewhere on your `PATH`:

```bash
cat > ~/.local/bin/groktocrawl <<'EOF'
#!/bin/sh
cd "$HOME/groktocrawl" || exit 1
exec uv run ./groktocrawl "$@"
EOF
chmod +x ~/.local/bin/groktocrawl
```

Or install `requests` into the Python that runs the script:

```bash
python3 -m pip install requests
```

```bash
./groktocrawl scrape <url>                      # Scrape a page to markdown
./groktocrawl search <query> --limit 5          # Search the web (default: general)
./groktocrawl search <query> --sources news     # Search news sources only
./groktocrawl search <query> --categories research  # Search with content category (mapped to SearXNG)
./groktocrawl search <query> --sources news --categories research  # Combined filter
./groktocrawl map <url> --limit 100             # Discover URLs on a site
./groktocrawl crawl <url> --max-depth 2         # Crawl a website
./groktocrawl agent "<prompt>"                  # Autonomous research agent
./groktocrawl --json --server <url> <cmd>       # JSON output, custom server
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/v2/scrape` | Scrape a single URL to clean markdown |
| POST | `/v2/agent` | Start an autonomous research agent |
| GET | `/v2/agent/:jobId` | Get agent job status and results |
| DELETE | `/v2/agent/:jobId` | Cancel an agent job |
| POST | `/v2/answer` | Grounded Q&A — search, synthesize, cite in one round-trip |
| GET | `/v2/activity` | List active/processing jobs across all job types |
| POST | `/v2/extract` | Extract structured data from URLs (with schema) |
| GET | `/v2/extract/:jobId` | Get extract status and results |
| POST | `/v2/crawl` | Crawl a website with full feature parity (path filtering, sitemap support, concurrency, dedup, webhooks, SSE streaming) |
| GET | `/v2/crawl/:jobId` | Get crawl status with pagination, timestamps, and per-page metadata |
| DELETE | `/v2/crawl/:jobId` | Cancel a crawl |
| GET | `/v2/crawl/:jobId/errors` | Get per-URL errors and robots-blocked URLs for a crawl |
| GET | `/v2/crawl/:jobId/stream` | SSE stream of crawl progress (reconnect to in-progress or completed crawls) |
| GET | `/v2/crawl/active` | List active/processing crawl jobs |
| POST | `/v2/crawl/params-preview` | Preview crawl parameters derived from a natural-language prompt |
| POST | `/v2/batch/scrape` | Scrape multiple URLs |
| POST | `/v2/search` | Search the web with content |
| POST | `/v2/map` | Discover URLs on a site |
| POST | `/v2/parse` | Upload a file (PDF, DOCX, PPTX, XLSX) and get markdown back |
| POST | `/v2/browser` | Create a headless browser session |
| GET | `/v2/browser` | List active browser sessions |
| POST | `/v2/browser/:id/execute` | Execute action (navigate, click, screenshot, etc.) |
| DELETE | `/v2/browser/:id` | Destroy a browser session |
| POST | `/v2/monitor` | Create a scheduled change monitor |
| GET | `/v2/monitor` | List all monitors |
| GET | `/v2/monitor/:id` | Get monitor status and history |
| PATCH | `/v2/monitor/:id` | Update monitor config |
| DELETE | `/v2/monitor/:id` | Delete a monitor |
| POST | `/v2/generate-llmstxt` | Generate an llms.txt file for a website |
| GET | `/v2/generate-llmstxt/:jobId` | Get generation status and result |

All Firecrawl v2 API-compatible in request/response shape.

### Search endpoint

`POST /v2/search` accepts Firecrawl v2's two-dimensional search model:

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | `string` | **Required.** Search query |
| `limit` | `int` | Max results (default: 5) |
| `sources` | `string[]` | Source type filter: `web`, `news`, `images`, `video`, `social` |
| `categories` | `string[]` | Content category: `research`, `github`, `pdf`, `news`, `science`, `it`, `general` |
| `contents` | `object` | Optional. Content extraction options per result. See [Contents Options](#contents-options) below. |
| `search_type` | `string` | Search mode: `fast` (default, <1s), `rich` (1-3s, scrapes + synthesizes) |
| `retrieval_mode` | `string` | `keyword` (default), `semantic`, `hybrid`, `vector`, `hybrid_vector` |
| `output_schema` | `object` | Optional JSON Schema for structured extraction |

**Contents Options:** When `contents` is provided, GroktoCrawl enriches each search result with the requested content:

| Field | Type | Description |
|-------|------|-------------|
| `text` | `bool` or `object` | `true` for full markdown, or dict with `verbosity` (`compact`/`standard`/`full`), `include`/`exclude` (section categories) |
| `highlights` | `bool` or `object` | `true` for auto highlights, or dict with `query` (focus) and `maxCharacters` |
| `summary` | `bool` or `object` | `true` for auto summary, or dict with `query` and `maxTokens` |
| `extras` | `object` | Dict with `links`, `imageLinks`, `codeBlocks` (max count each) |

Section categories for `include`/`exclude`: `header`, `navigation`, `banner`, `body`, `sidebar`, `footer`, `metadata`.
Both `sources` and `categories` are translated to SearXNG-native categories and can be combined:

| Firecrawl value | Mapped to SearXNG |
|----------------|-------------------|
| `sources=news` | `categories=news` |
| `sources=images` | `categories=images` |
| `sources=web` | `categories=general` |
| `categories=research` | `categories=science` |
| `categories=github` | `categories=it` |
| `categories=pdf` | `categories=general` |

Unknown values pass through to SearXNG as-is for forward compatibility. When neither
`sources` nor `categories` is specified, defaults to `general`.

Results are grouped by source type in the response:
```json
{"data": {"web": [...], "images": [], "news": []}}
```

### Agent endpoint

The `POST /v2/agent` endpoint accepts an optional `model` field to override the environment-configured LLM on a per-request basis:

```json
{
  "prompt": "Research the latest AI safety papers",
  "model": "gpt-4o"
}
```

When `model` is omitted or set to `"default"`, the `LLM_MODEL` from `.env` is used. This is useful for routing simple lookups to a cheaper model and complex research to a more capable one.

The agent is powered by a **determined research prompt** that evaluates source quality, synthesizes across multiple pages, detects contradictions, and cites sources by URL. It does not fabricate information — if the available sources don't answer the question, it says so and suggests what would be needed.

## Web Portal

A browser-based UI is available at `http://localhost:8082` when `portal-svc` is running. It provides a chat interface to the agent and answer endpoints, showing source citations and streaming responses inline. The portal is a thin client — all requests route through the agent API.

## OpenAPI / Swagger Docs

Interactive API documentation is available when the stack is running:

- **Swagger UI**: [`http://localhost:8080/docs`](http://localhost:8080/docs)
- **Raw OpenAPI spec**: [`http://localhost:8080/openapi.json`](http://localhost:8080/openapi.json)

The spec is auto-generated by FastAPI from the route handlers and Pydantic models — always up to date with the running code. All 17+ endpoints with request/response schemas are documented.

## Comparison to Firecrawl

| Feature | Firecrawl Cloud | Firecrawl Self-Hosted | GroktoCrawl |
|---------|----------------|----------------------|-------------|
| Scrape / Crawl / Map / Search | ✅ | ✅ | ✅ |
| Crawl: path filtering (include/exclude) | ✅ | ✅ | ✅ |
| Crawl: sitemap modes (include/skip/only) | ✅ | ❌ | ✅ |
| Crawl: configurable concurrency & delay | ✅ | ✅ | ✅ |
| Crawl: per-page webhooks & SSE streaming | ✅ | ❌ | ✅ |
| Crawl: NL-to-params preview | ✅ | ❌ | ✅ |
| Crawl: content dedup (canonical + hash) | ✅ | ❌ | ✅ |
| Crawl: maxAge/minAge caching | ✅ | ❌ | ✅ |
| Crawl: errors & active endpoints | ✅ | ❌ | ✅ |
| Agent endpoint | ✅ | ❌ | ✅ |
| Extract (schema-based) | ✅ | ❌ | ✅ |
| Browser sessions | ✅ | ❌ | ✅ |
| Scheduled monitors | ✅ | ❌ | ✅ |
| Parse (PDF, DOCX) | ✅ | ✅ | ✅ |
| Generate llms.txt | ❌ | ❌ | ✅ |
| Webhook delivery | ✅ | ❌ | ✅ |
| License | Proprietary | AGPL-3.0 | **MIT** |
| Self-contained Docker | ❌ | ✅ | ✅ |
| LLM integration | Built-in | Requires API key | BYO or fixture |
| **Beyond Firecrawl** | | | |
| Semantic search / vector index | ❌ | ❌ | ✅ |
| Grounded Q&A (/v2/answer) | ❌ | ❌ | ✅ |
| Web portal for human users | ❌ | ❌ | ✅ |
| Site adapters (GitHub, Substack, Reddit, YouTube, Bluesky, Gutenberg, Shopify) | ❌ | ❌ | ✅ |
| Intelligent scrape cache (ETag/Last-Modified) | ❌ | ❌ | ✅ |
| Politeness protocol (robots.txt, rate limiting) | ❌ | ❌ | ✅ |
| Proxy support | ❌ | ❌ | ✅ |
| Agent SSE streaming | ❌ | ❌ | ✅ |
| Search type spectrum (fast / rich / structured) | ❌ | ❌ | ✅ |
| Artifact-pyramid CLI output | ❌ | ❌ | ✅ |
| Richer content extraction (verbosity, sections, highlights, summaries, extras) | ❌ | ❌ | ✅ |

## AgentSkills Compatibility

GroktoCrawl ships as an [AgentSkills](https://agentskills.io)-compatible skill at `skills/groktocrawl/`. Any agent that supports the AgentSkills format (Claude Code, Cursor, etc.) can load it:

```
skills/groktocrawl/
├── SKILL.md                  # Metadata + instructions
├── scripts/groktocrawl       # CLI — all endpoints
├── references/triggers.md    # When to use which command
└── assets/examples.md        # Usage examples
```

The skill bundles the CLI directly — no additional setup required beyond having the repo on disk.

## Hermes Agent Considerations

If you use Hermes Agent, GroktoCrawl replaces the built-in `web_search` and `web_extract` tools with more capable alternatives. To avoid competition between tools:

### Disable the `web` toolset

Remove `web` from `default_toolsets` and `platform_toolsets.cli` in `~/.hermes/config.yaml`:

```yaml
# Before
default_toolsets:
  - terminal
  - file
  - web              # ← remove

# After
default_toolsets:
  - terminal
  - file
```

This removes `web_search` and `web_extract` from your agent's toolset. All web tasks will route through `groktocrawl` instead.

### Install the CLI

The CLI is at `groktocrawl` in the repo root. Copy it to your PATH:

```bash
cp groktocrawl ~/.local/bin/
```

### Install the AgentSkills skill

The bundled skill at `skills/groktocrawl/` follows the [AgentSkills](https://agentskills.io) spec. Symlink it into your Hermes skills directory:

```bash
ln -sf "$PWD/skills/groktocrawl" ~/.hermes/skills/
```

Then load it in-session with `/skill groktocrawl` or preload it via `hermes -s groktocrawl`.

### Environment variables

The CLI discovers the server in this order:
1. `--server <url>` flag
2. `GROKTOCRAWL_API_URL` env var
3. `FIRECRAWL_API_URL` env var (backward compat)
4. `~/.hermes/.env` file
5. Default: `http://localhost:8080`

Add to `~/.hermes/.env` if your instance runs elsewhere:
```env
GROKTOCRAWL_API_URL=http://localhost:8080
```

## Security

### API Authentication (recommended for production)

Set `API_KEY` in your `.env` file to enable bearer token authentication:

```env
API_KEY=sk-your-secret-key-here
```

Once set, every API call must include an `Authorization` or `X-API-Key` header:

```bash
curl -X POST http://localhost:8080/v2/scrape \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-api-key-here"  # gitleaks:allow
  -d '{"url": "https://example.com"}'

# Or via CLI:
groktocrawl --api-key sk-your-secret-key-here scrape https://example.com
```

When **no** `API_KEY` is configured, the API is fully open (backward
compatible). Each response includes an `X-Security-Warning` header and
the `/health` endpoint adds a `security` field to warn callers.

### Private Network Protection

The built-in browser and scraper services block navigation to private IPs
(RFC 1918), loopback addresses, cloud metadata endpoints, and the Docker
host machine. This prevents SSRF-based pivoting through the headless
browser. The blocklist applies to both direct URLs and resolved hostnames
(DNS rebinding protection).

### Service Architecture

Only the **agent API** (port `8080`) is exposed to the host. Internal
services (`browser-svc`, `scraper-svc`, `parse-svc`) are reachable only
via Docker internal DNS — they do not publish host ports. All requests
route through the agent API.

### Reporting Vulnerabilities

See [SECURITY.md](SECURITY.md) for our disclosure policy and how to
privately report security issues.

## Proxy Configuration

GroktoCrawl supports outbound proxy routing via the `SCRAPER_PROXY_URL` environment variable. When set, all scrape requests route through the specified proxy before reaching their target.

```env
SCRAPER_PROXY_URL=http://user:pass@residential-proxy:8080
```

**Supported schemes:** `http://`, `https://`, `socks5://`, `socks5h://`

**Behavior:**
- The proxy is applied at the transport layer across the full scrape pipeline — httpx clients (Tiers 1-2) and Playwright browser context (Tier 3)
- If the proxy is unreachable, Groktocrawl **fails open**: it retries the request without a proxy and logs the fallback at WARN level
- Every proxied scrape records `proxy_host=<host:port>` in its structured log for operational debugging

**Guardrails:**
- **Opt-in only** — users who don't set this variable see zero behavioral change
- **Single static proxy** — one URL only. For proxy rotation or pool management, front Groktocrawl with a rotating proxy orchestrator (HAProxy, Scrapoxy, etc.)
- **Credentials never logged** — only the host and port are recorded in scrape logs; the full URL (including auth) is redacted at the logging boundary

**Notes:**
- Proxy credentials embedded in the URL use standard HTTP Basic Auth encoding. Avoid special characters (@, #, %) in passwords — they can conflict with URL parsing.
- The Playwright proxy uses **context-level assignment** (`browser.new_context(proxy=...)`) for per-job isolation, not launch-level args (`--proxy-server`).

See [ADR-0020](docs/adr/0020-proxy-support-with-guardrails.md) for the full architecture decision.

## Adapters

GroktoCrawl supports **site-specific content handlers** that extract richer content from JavaScript-heavy sites. When `scrape <url>` is called, the adapter registry checks if a handler matches the URL before running the generic pipeline. If it matches, the adapter handles extraction with its own fallback chain. If it fails, the generic pipeline runs as normal.

### YouTube Adapter

`scrape <youtube-url>` returns a markdown document with:

- **YAML frontmatter:** video_id, title, channel, channel_url, thumbnail_url, source
- **Markdown body:** full video transcript

**Fallback chain:** youtube_transcript_api (free, no key) → browser render + DOM extraction

**Configuration:**

| Variable | Default | Description |
|---|---|---|
| `ADAPTER_YOUTUBE_API_KEY` | *(none)* | YouTube Data API v3 key (optional — transcript works without it) |

### Bluesky Adapter

`scrape <bsky.app-url>` returns a markdown document with:

- **YAML frontmatter:** author, handle, did, post_id, timestamp, reply_count, like_count, repost_count
- **Markdown body:** post text + thread replies

**Fallback chain:** AT Protocol XRPC API (public, no auth) → browser render + DOM extraction

**Configuration:** None — the public API requires no authentication.

### Substack Adapter

`scrape <substack-url>` returns a markdown document with:

- **YAML frontmatter:** title, author, publication, published_date, source
- **Markdown body:** full article text in clean markdown

**Fallback chain:** RSS feed (fast, structured, no auth) → readability-lxml page extraction → browser render

**Configuration:** None — Substack requires no API keys.

**Vanity domain detection:** The adapter automatically detects Substack-hosted publications behind custom domains (e.g. `www.lennysnewsletter.com`) by probing `{domain}/feed` for the Substack RSS generator tag. Results are cached per-domain for 1 hour.

### GitHub Adapters

Two adapters handle different URL types on `github.com`, working together via priority dispatch:

| Priority | Adapter | Handles | Primary Strategy |
|----------|---------|---------|-----------------|
| 200 | GitHub File | raw files, blobs, READMEs, directory listings | raw.githubusercontent.com direct fetch |
| 190 | GitHub Social | issues, PRs, discussions, releases, commits | GraphQL API (v4) |

`scrape <github-url>` returns structured markdown with YAML frontmatter containing owner, repo, and type-specific metadata.

**Resource coverage:**

| URL Pattern | Handled By | Features |
|---|---|---|
| `raw.githubusercontent.com/*` | File adapter | Raw content, no rate limit |
| `github.com/*/blob/*` | File adapter | Rewrites to raw URL |
| `github.com/*` (repo root) | File adapter | README + stars/forks/language/topics |
| `github.com/*/tree/*` | File adapter | Directory listing, items sorted dirs-first |
| `github.com/*/issues/{n}` | Social adapter | Body, comments, labels, state, milestone |
| `github.com/*/pull/{n}` | Social adapter | Body, reviews, diff stats, changed files, merge status |
| `github.com/*/discussions/{n}` | Social adapter | Category, upvotes, answer, comments |
| `github.com/*/releases/tag/{v}` | Social adapter | Release notes, assets, download URLs |
| `github.com/*/releases` | Social adapter | Releases list with descriptions |
| `github.com/*/commit/{sha}` | Social adapter | Message, author, associated PRs |

**Fallback chains:**

- **File adapter:** raw.githubusercontent.com direct fetch → GitHub Contents API → generic tier
- **Social adapter:** GitHub GraphQL API (single query) → GitHub REST API → HTML page scrape (readability) → generic tier

**Configuration:**

The `GITHUB_TOKEN` environment variable enables authenticated access:

| Variable | Default | Effect |
|----------|---------|--------|
| `GITHUB_TOKEN` | *(none)* | 5,000 API req/hr vs 60/hr unauth; enables GraphQL; always falls back to HTML scrape |

A token with `public_repo` scope is sufficient for public repositories. For private repos, use `repo` scope. Without a token, the file adapter works fully and the social adapter falls back to REST (60 req/hr) then HTML scrape — every URL type returns useful content.

### Greenhouse Adapter

`scrape <boards.greenhouse.io-url>` returns a markdown document with:

- **YAML frontmatter:** title, company, location, department, requisition_id, employment_type, date_posted, apply_url
- **Markdown body:** full job description converted from HTML to clean markdown

**Fallback chain:** Greenhouse Boards API (`boards-api.greenhouse.io`) → readability-lxml page scrape

**URL patterns:**
- `boards.greenhouse.io/{company}/jobs/{id}` — individual job page
- Any URL with `?gh_jid={id}` query parameter (auto-discovers company via embed page)

**Configuration:** None — the public API requires no authentication.

### AshbyHQ Adapter

`scrape <jobs.ashbyhq.com-url>` returns a markdown document with:

- **YAML frontmatter:** id, title, department, team, location, workplace_type, employment_type, published_date, requisition_id, remote status, compensation summary
- **Markdown body:** job description converted from HTML to clean markdown

**Fallback chain:** SSR-embedded `window.__appData` JSON extraction (no API calls) → readability-lxml page scrape

**URL patterns:**
- `jobs.ashbyhq.com/{company}` — listing page (renders a table of all postings)
- `jobs.ashbyhq.com/{company}/{uuid}` — individual job page

**Configuration:** None — AshbyHQ requires no API keys.

### Shopify Adapter

`scrape <shopify-url>` extracts blog/article content from Shopify-hosted stores, bypassing the UCP (Universal Commerce Protocol) content-negotiation trap.

| Feature | Details |
|---------|---------|
| **URL patterns** | `/blogs/`, `/products/`, `/collections/`, `/pages/` on any domain |
| **Fallback chain** | readability-lxml with browser UA → Playwright render → generic tier |
| **Config** | None — no API key required |

Shopify stores return an agent instruction page when they receive `Accept: text/markdown` (UCP commerce API guide). This adapter fetches the page HTML with a standard browser User-Agent and extracts content via readability-lxml, bypassing the trap entirely.

**Configuration:** None — Shopify storefronts serve public content without authentication.

### Adding a New Adapter

1. Create `scraper-svc/scraper/adapters/<site>.py`
2. Subclass `SiteAdapter`, set `name`, `patterns`, `priority`, implement `scrape()`
3. Decorate with `@adapter` for auto-registration
4. Add any new dependencies to `scraper-svc/pyproject.toml`
5. Add `.env` variables to `.env.sample` and document them in this section

See `docs/adr/` for the architecture decision records behind the adapter pattern, and `CONTRIBUTING.md` for the ADR convention.

> **External anti-detection browser hook:** For sites that require advanced browser fingerprinting evasion (Turnstile challenges, DDoS-Guard), the adapter framework supports routing scrape requests to a self-hosted anti-detection browser service. The `AdapterContext.config` provides access to environment variables for configuring the external service endpoint. This is an **advanced operator pattern** — the external browser service is self-managed and outside the project's scope. Groktocrawl provides the dispatch interface; the external service's behavior, compatibility, and reliability are the operator's responsibility.

## Project Status

Active development. All core Firecrawl v2 API endpoints implemented and integration-tested. See [issues](https://github.com/groktopus/groktocrawl/issues) for upcoming features. Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
