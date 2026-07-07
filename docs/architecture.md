# GroktoCrawl Architecture

## System Context

```mermaid
flowchart LR
    user("User / CLI")
    agent_api("GroktoCrawl Agent API\n[FastAPI] Port 8080")
    scraper_svc("Scraper Service\n[Python] Port 8001")
    browser_svc("Browser Service\n[Playwright]")
    semantic_svc("Semantic Service\n[BGE-M3 / Reranker]\nPort 8003 (internal)")
    qdrant("Qdrant\n[Vector DB]")
    valkey("Valkey\n[Key-Value Store]")
    searxng("SlopSearX\n[Search Engine]")
    llm_svc("LLM Service\n[OpenAI Compatible]")
    flare_solverr("FlareSolverr\n[Cloudflare Solver]")
    portal_svc("Portal Service\n[Web UI] Port 8082")
    parse_svc("Parse Service\n[Markdown Parser]")

    user -->|"CLI / curl / SDK"| agent_api
    user -->|"Browser"| portal_svc
    agent_api -->|"/v2/scrape"| scraper_svc
    agent_api -->|"/v2/search"| searxng
    agent_api -->|"embed / rerank / index"| semantic_svc
    agent_api -->|"/v2/agent"| llm_svc
    agent_api <-->|"Job status"| valkey
    portal_svc -->|"Proxy to"| agent_api
    semantic_svc --> qdrant
    scraper_svc --> browser_svc
    scraper_svc --> flare_solverr
    scraper_svc --> llm_svc
    parse_svc --> llm_svc

    style user fill:#084,color:#fff
    style agent_api fill:#06c,color:#fff
    style scraper_svc fill:#06c,color:#fff
    style browser_svc fill:#06c,color:#fff
    style semantic_svc fill:#06c,color:#fff
    style portal_svc fill:#06c,color:#fff
    style parse_svc fill:#06c,color:#fff
    style valkey fill:#963,color:#fff
    style searxng fill:#963,color:#fff
    style qdrant fill:#963,color:#fff
    style llm_svc fill:#639,color:#fff
    style flare_solverr fill:#639,color:#fff
```

## Container Diagram (internal services)

```mermaid
flowchart LR
    subgraph agent_svc["Agent Service (FastAPI)"]
        api_routes["API Routes\napi.py"]
        worker["Async Worker\nworker.py"]
        research["Research Agent\nresearch.py"]
        scraper_client["Scraper Client\nscraper_client.py"]
        searxng_client["SearXNG API Client\nsearxng_client.py"]
        semantic_client["Semantic Client\nsemantic_client.py"]
        webhook["Webhook Deliverer\nwebhook.py"]
    end

    subgraph scraper_svc["Scraper Service (FastAPI)"]
        smart_scrape["Smart Scrape\nfetch.py"]
        tier1["Tier 1: llms.txt"]
        tier2["Tier 2: Content Neg."]
        tier3["Tier 3: Playwright"]
        tier35["Tier 3.5: FlareSolverr"]
        tier4["Tier 4: LLM Recovery"]
        adapters["Adapter Registry\nadapters/"]
        youtube_ad["YouTube Adapter"]
        bluesky_ad["Bluesky Adapter"]
    end

    subgraph semantic_svc["Semantic Service (FastAPI)"]
        embed["POST /embed\nBGE-M3 / configurable"]
        rerank["POST /rerank\nCross-Encoder"]
        index["POST /index\nStore in Qdrant\nNamed vectors"]
        search_vector["POST /search/vector\nQuery Qdrant\nActive named vector"]
        stats["GET /index/stats"]
        model_info["GET /index/model\nModel config + migration"]
        migrate["POST /index/migrate/start\nGET /index/migrate/status\nPOST /index/migrate/cutover"]
        metrics_ep["GET /metrics\nOpenMetrics format\nstdlib (no deps)"]
    end

    smart_scrape --> tier1
    tier1 --> tier2
    tier2 --> tier3
    tier3 --> tier35
    tier35 --> tier4
    smart_scrape -.-> adapters
    adapters --> youtube_ad
    adapters --> bluesky_ad

    agent_svc --> scraper_svc
    agent_svc --> semantic_svc
    semantic_svc --> qdrant[(Qdrant\nVector DB)]
```

## Search Retrieval Pipeline

GroktoCrawl supports five retrieval modes, controlled by the `retrieval_mode` field on `POST /v2/search`:

| Mode | Pipeline | Latency | Phase |
|---|---|---|---|
| `keyword` | SlopSearX only | <1s | — |
| `semantic` | SlopSearX → scrape → BGE-M3 embed → cosine rerank | 1–30s | Phase 1 |
| `hybrid` | SlopSearX → scrape → cross-encoder merge | 2–40s | Phase 1 |
| `vector` | Qdrant only → embed query → vector search | <1s | Phase 2 |
| `hybrid_vector` | SlopSearX + Qdrant parallel → merge → dedup by URL | 1–30s | Phase 2 |

```mermaid
flowchart TD
    Q[User Query]
    Q --> M{retrieval_mode}
    M -->|keyword| SRX[SlopSearX]
    M -->|semantic| SRX2[SlopSearX] --> SCR2[Scrape] --> EMB2[BGE-M3 Embed] --> COS[Cosine Rerank]
    M -->|hybrid| SRX3[SlopSearX] --> SCR3[Scrape] --> XENC[Cross-Encoder Merge]
    M -->|vector| EMB4[BGE-M3 Embed Query] --> QDR4[(Qdrant Search)]
    M -->|hybrid_vector| PAR{Parallel}
    PAR --> SRX5[SlopSearX]
    PAR --> EMB5[BGE-M3 Embed] --> QDR5[(Qdrant)]
    SRX5 --> MERGE[Merge + URL Dedup]
    QDR5 --> MERGE

    SRX --> RESP[Results]
    COS --> RESP
    XENC --> RESP
    QDR4 --> RESP
    MERGE --> RESP
```

## Indexing Pipeline

Every scrape, crawl, and map operation indexes the page in Qdrant via a fire-and-forget hook:

```mermaid
flowchart TD
    SCRAPE[Scrape / Crawl / Map<br/>complete] --> HOOK[agent-svc<br/>indexing hook]
    HOOK --> SVC[semantic-svc<br/>POST /index]
    SVC --> EMBED[BGE-M3 embed<br/>content[:2000]]
    EMBED --> CAT[Domain classification<br/>news / docs / reference / ...]
    CAT --> PAYLOAD[Enrich payload<br/>crawl_count, access_count,<br/>first_indexed_at, domain_category]
    PAYLOAD --> UPSERT[Qdrant upsert<br/>uint64 point ID from URL hash]
    UPSERT --> CHECK{docs > 250K?}
    CHECK -->|yes| SCORE[Score-based eviction<br/>retention_score = domain_mult × recency<br/>+ access_boost + crawl_boost]
    SCORE --> EVICT[Delete lowest-scored docs]
    CHECK -->|no| DONE[✓ indexed]
    EVICT --> DONE

    subgraph AccessTracking [Search Access Tracking]
        SEARCH[POST /search/vector] --> ACCESS[Fire-and-forget<br/>increment access_count<br/>update last_accessed_at]
    end
```

Indexing is best-effort — failure never blocks the scrape/crawl job. The same URL re-indexed updates the existing vector rather than creating a duplicate.

**Retention scoring** (Phase 3): When the index exceeds capacity, all points are scored by a composite function:
- `domain_multiplier`: 0.3 (news) – 1.2 (docs), based on domain classification
- `recency_factor`: decays exponentially from 1.0 (today) to 0.1 (90+ days)
- `access_boost`: up to 1.0 for frequently returned search results
- `crawl_boost`: up to 1.0 for frequently re-crawled pages

The lowest-scored documents are evicted. News and social content evicts first; reference and docs content persists longest.

## Available Adapters

| Adapter | Source | Fallback Chain | Docs |
|---------|--------|----------------|------|
| YouTube | `adapters/youtube.py` | youtube_transcript_api → browser render | ADR-0001–0009 |
| Bluesky | `adapters/bluesky.py` | AT Protocol API → browser render | ADR-0001–0009 |

## Architecture Decision Records

All significant architectural decisions are documented as ADRs in `docs/adr/`. See the [ADR index](adr/README.md) for the full list. Key ADRs for this architecture:

| ADR | Decision |
|-----|----------|
| [ADR-0013](adr/0013-search-architecture-with-vertical-categories.md) | Search architecture with vertical categories |
| [ADR-0017](adr/0017-grounded-qa-endpoint.md) | Grounded Q&A endpoint |
| [ADR-0023](adr/0023-search-type-spectrum-fast-and-rich.md) | Search type spectrum (fast/rich) |
| [ADR-0025](adr/0025-semantic-search-pipeline.md) | Phase 1 semantic reranking |
| [ADR-0026](adr/0026-phase2-vector-index.md) | Phase 2 persistent vector index |
| [ADR-0027](adr/0027-smarter-index-retention.md) | Phase 3 smarter index retention |
| [ADR-0028](adr/0028-embedding-model-migration-path.md) | Phase 4 embedding model migration |
| [ADR-0029](adr/0029-service-level-metrics-for-semantic-svc.md) | Service-level metrics for semantic-svc |
| [ADR-0018](adr/0018-observability-infrastructure.md) | Observability infrastructure (agent-svc /metrics, health, logging) |
