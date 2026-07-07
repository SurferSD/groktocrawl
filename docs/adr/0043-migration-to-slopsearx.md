# Migration from SearXNG to SlopSearX

* Status: accepted
* Deciders: magnus
* Date: 2026-07-07

Technical Story: GroktoCrawl originally used SearXNG as its self-hosted search backend. As the project evolved, SearXNG's broad scope (supporting 100+ engines, multiple frontend themes, preferences, user accounts) introduced operational overhead disproportionate to GroktoCrawl's needs — which required only the `/search` JSON API. SlopSearX (`ghcr.io/magnus919/slopsearx:latest`) implements that same SearXNG JSON API surface but strips away everything unrelated to programmatic search, reducing image size, configuration surface, and operational complexity.

## Context and Problem Statement

GroktoCrawl's search architecture (ADR-0013), search type spectrum (ADR-0023), semantic search pipeline (ADR-0025), and vector index (ADR-0026) all depend on a SearXNG-compatible JSON API for keyword retrieval. SearXNG satisfied this dependency but carried significant unused weight:

- **Image size:** SearXNG's Docker image includes a full web UI, theme engine, user preference system, and 100+ engine integrations that GroktoCrawl never used.
- **Configuration surface:** Settings files, brand customization, engine enable/disable toggles, and rate-limit configurations were irrelevant for programmatic-only access.
- **Operational overhead:** Updates to SearXNG could introduce changes to the UI, theming, or engine integrations that had no impact on GroktoCrawl but still required compatibility verification.

The search backend needed only one thing: respond to `GET /search?q=<query>&format=json&categories=<categories>` with a SearXNG-formatted JSON result set. SlopSearX was built specifically for this use case — a focused, lightweight implementation of the SearXNG JSON API with no UI, no themes, and a minimal configuration surface.

## Decision Drivers

* Maintain API compatibility — all existing code (`searxng_client.py`, `SearXNGClient`, `SEARXNG_URL`) must work without modification.
* Reduce operational complexity — smaller image, fewer configuration knobs, faster startup.
* Self-hosted — no external search API dependency.
* Preserve search quality — keyword search results must remain comparable to SearXNG.

## Considered Options

### A. Migrate to SlopSearX *(chosen)*

Replace the `searxng` Docker service with `slopsearx` (`ghcr.io/magnus919/slopsearx:latest`). No source code changes — `searxng_client.py` continues to use the SearXNG JSON API which SlopSearX implements.

**Positive:**
- Zero code changes — full API compatibility with the SearXNG JSON endpoint.
- Smaller image footprint — no UI, no theming, no user management.
- Faster startup — fewer services to initialize.
- Purpose-built for programmatic search — no unused features to maintain or verify.
- All existing documentation, ADRs, and configuration remain conceptually valid (the API contract is unchanged).

**Negative:**
- New container image to track and update.
- SlopSearX is a newer project with a smaller community than SearXNG.
- Potential for minor behavioral differences in edge cases (category filtering, pagination).

### B. Stay on SearXNG *(rejected)*

Continue using the full SearXNG image.

**Positive:**
- Familiar, established project with large community.
- Broad engine support for edge cases.

**Negative:**
- Continued operational overhead from unused features.
- Larger attack surface from UI, preferences, and admin interfaces.
- Configuration complexity disproportionate to GroktoCrawl's needs.
- Rejected: the operational cost of maintaining a full SearXNG instance exceeds the benefit for a programmatic-only consumer.

### C. Switch to an external search API (Google, Bing, Brave) *(rejected)*

Replace SearXNG with a commercial search API.

**Positive:**
- Potentially higher result quality.
- Zero infrastructure to manage.

**Negative:**
- Violates GroktoCrawl's self-hosted design principle.
- Recurring cost per query.
- Network dependency on external service availability.
- Privacy: all queries leave the self-hosted environment.
- Rejected: self-hosted search is a core architectural commitment.

## Decision Outcome

Chosen option: **A. Migrate to SlopSearX**, replacing the `searxng` Docker service with `slopsearx` while preserving API compatibility at the source code level.

### What Changes

| Layer | Change |
|-------|--------|
| Docker service | `searxng` → `slopsearx` (`ghcr.io/magnus919/slopsearx:latest`) |
| Environment variable | `SEARXNG_URL` unchanged (SlopSearX exposes the same JSON API at the same path) |
| Source code | No changes — `searxng_client.py`, `SearXNGClient` class, and all internal identifiers are preserved |
| Documentation | All prose references to the search backend updated from "SearXNG" to "SlopSearX" |
| ADRs | ADR-0013 (search architecture) superseded by this ADR; remaining ADRs preserved as historical records |

### What Does NOT Change

- **Source code identifiers:** `searxng_client.py`, `SearXNGClient`, `SEARXNG_URL` remain as-is. SlopSearX implements the SearXNG JSON API, so these identifiers are still correct — they name the API contract, not the implementation.
- **API surface:** All endpoints (`/v2/search`, `/v2/answer`, `/v2/agent`, `/v2/crawl`) are unchanged.
- **Search behavior:** Keyword search, category filtering, and response format are preserved.
- **CHANGELOG.md:** Historical release notes are factual records and are not retroactively updated.

## Rationale

The SearXNG JSON API is the contract, not SearXNG itself. By replacing the implementation while preserving the contract, GroktoCrawl reduces operational complexity without any code changes. This is the same pattern as replacing a database implementation behind a standard protocol — the client code speaks the protocol, not the specific server.

SlopSearX was built for exactly this use case: a minimal, focused search backend that speaks the SearXNG JSON API and nothing else. It strips away the UI, preferences, admin panel, and engine management that SearXNG carries — all features that GroktoCrawl never used.

## Consequences

### Positive

- **Smaller deployment footprint:** SlopSearX image is significantly smaller than SearXNG.
- **Faster startup:** Fewer services to initialize on container start.
- **Reduced attack surface:** No web UI, admin panel, or user management endpoints.
- **Simplified configuration:** No settings files, brand customization, or engine toggles to maintain.
- **Zero code changes:** Full backward compatibility with all existing code.

### Neutral

- **ADR-0013 superseded:** The search architecture decision record is updated to reflect the migration. The architectural patterns it documents (vertical categories, response format) remain valid — only the search backend implementation changed.
- **ADRs 0023, 0025, 0026 preserved:** These ADRs document search features that depend on the SearXNG JSON API contract. Since SlopSearX implements that contract, the ADRs remain accurate — they describe the API contract, not the specific implementation.

### Negative

- **New project dependency:** SlopSearX is newer and has a smaller community than SearXNG.
- **Edge case risk:** Minor behavioral differences in category filtering or pagination could surface over time.

## Links

* Supersedes: [ADR-0013](0013-search-architecture-with-vertical-categories.md) — search architecture, originally built on SearXNG, now backed by SlopSearX
* References: [ADR-0023](0023-search-type-spectrum-fast-and-rich.md) — search type spectrum depends on the SearXNG-compatible JSON API
* References: [ADR-0025](0025-semantic-search-pipeline.md) — semantic reranking pipeline uses SearXNG-compatible keyword retrieval
* References: [ADR-0026](0026-phase2-vector-index.md) — vector index operates alongside SearXNG-compatible keyword search
* SlopSearX: [ghcr.io/magnus919/slopsearx](https://github.com/magnus919/slopsearx)
