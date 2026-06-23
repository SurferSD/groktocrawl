# GroktoCrawl — Open PR CI Failure Report

Generated: 2026-06-19 | 21 open PRs (#291–#311)

## 1. All Open PRs and Their Status

### Stacked Crawl Feature PRs (linear dependency chain)

| PR | Title | Branch | Int Test | Dead Code | Gitleaks | Droid Review | Review |
|----|-------|--------|----------|-----------|----------|-------------|--------|
| #293 | crawl-engine-core | feature/crawl-engine-core | ❌ | ✅ | ✅ | SKIP | — |
| #294 | path-filtering | feature/path-filtering | ❌ | ✅ | ✅ | SKIP | — |
| #295 | crawl-status-response-enhancement | feature/…enhancement | ❌ | ✅ | ✅ | SKIP | — |
| #296 | crawl-metrics | feature/crawl-metrics | ❌ | ✅ | ✅ | SKIP | — |
| #297 | crawl-cli-update | feature/crawl-cli-update | ❌ | ✅ | ✅ | SKIP | — |
| #298 | sitemap-parser | feature/sitemap-parser | ❌ | ✅ | ✅ | SKIP | — |
| #299 | domain-scope-controls | feature/domain-scope-controls | ❌ | ✅ | ✅ | SKIP | — |
| #300 | crawl-politeness-integration | feature/…politeness-integration | ❌ | ✅ | ✅ | ❌ | — |
| #301 | content-dedup | feature/content-dedup | ❌ | ✅ | ✅ | ❌ | — |
| #302 | crawl-cache | feature/crawl-cache | ❌ | ✅ | ✅ | ❌ | — |
| #303 | crawl-active-endpoint | feature/crawl-active-endpoint | ❌ | ✅ | ✅ | ❌ | — |
| #304 | fix-crawl-cache-combined-semantics | feature/fix-crawl-cache-… | ❌ | ✅ | ✅ | ❌ | — |
| #305 | nl-to-params | feature/nl-to-params | ❌ | ✅ | ✅ | ❌ | — |
| #306 | crawl-per-page-webhooks | feature/crawl-per-page-webhooks | ❌ | ❌ | ✅ | ❌ | — |
| #307 | crawl-sse-streaming | feature/crawl-sse-streaming | ❌ | ❌ | ✅ | ❌ | — |
| #308 | crawl-advanced-scrape-options | feature/crawl-advanced-scrape-options | ❌ | ❌ | ✅ | ❌ | — |
| #309 | crawl-integration-tests | feature/crawl-integration-tests | ❌ | ❌ | ❌ | ❌ | — |
| #310 | fix-sse-error-events | feature/fix-sse-error-events | ❌ | ❌ | ❌ | ❌ | — |
| #311 | fix-integration-test-docker-skips | feature/fix-integration-test-docker-skips | ❌ | ❌ | ❌ | ❌ | — |

### Standalone PRs

| PR | Title | Branch | Int Test | Dead Code | Gitleaks | Droid Review | Review |
|----|-------|--------|----------|-----------|----------|-------------|--------|
| #292 | refactor-llmstxt-to-linkextractor | feature/refactor-llmstxt-… | ❌ | ✅ | ✅ | SKIP | — |
| #291 | release 0.9.0 | release-please--branches--main | N/A | N/A | N/A | N/A | — |

---

## 2. Specific Failures per PR with Root Cause

### A. Integration Tests — ALL PRs #292–#311

**Error:** `RuntimeError: Timed out waiting for test-site`

**Root Cause:** PORT MISMATCH in CI workflow. The `docker-compose.yml` maps test-site to host port **8005** (`"8005:8000"`), but the CI health check at `.github/workflows/docker.yml` (the "Wait for test-site" step) probes `http://localhost:8000/health` — port **8000**. No service listens on port 8000, so the health check always times out after 60 seconds.

This bug was introduced in **PR #288** (commit `dd3e06d`) which added the test-site health check step to `docker.yml`. It checks the wrong port. The bug was never noticed because:
- Before #288, test-site health was not explicitly checked
- After #288, it started failing for every subsequent PR

**Fix required:** Change the health check URL in `.github/workflows/docker.yml` from `http://localhost:8000/health` to `http://localhost:8005/health`.

**Affected files:** `.github/workflows/docker.yml` line in "Wait for test-site" step.

---

### B. Dead Code (vulture) — PRs #306–#311

**Error:** `agent-svc/agent/webhook.py:59: unused variable 'webhook_id_key' (100% confidence)`

**Root Cause:** PR #306 (feature/crawl-per-page-webhooks) added a `webhook_id_key: str | None = None` parameter to the `deliver_webhook()` function. The parameter is documented as "Deprecated — UUID v4 is always unique" but is never actually used in the function body. Vulture detects this with 100% confidence.

The webhook.py changes propagate to all descendant stacked PRs (#306–#311).

**Fix options (prefer A):**
- **A:** Remove the deprecated `webhook_id_key` parameter entirely from the function signature
- **B:** Prefix with underscore (`_webhook_id_key`) to signal intentional non-use (vulture ignores `_`-prefixed vars)
- **C:** Add `# noqa` or a vulture whitelist entry

**Affected files:** `agent-svc/agent/webhook.py` (introduced in PR #306, inherited by #307–#311). Fix once in #306 and rebase descendants.

---

### C. Gitleaks (secret scanning) — PRs #309–#311

**Error:** `curl-auth-header` false positive in `README.md:344`

**Root Cause:** The `.gitleaksignore` file has an exception for `README.md:curl-auth-header:333`, but PRs #309–#311 added new content to the README (crawl endpoint documentation), shifting the `Authorization: Bearer sk-your-secret-key-here` documentation example from line ~335 to line 344. The line number in the `.gitleaksignore` fingerprint no longer matches.

The documentation example (`Authorization: Bearer sk-your-secret-key-here`) is a legitimate placeholder in API usage docs — not a real secret.

**Fix:** Update `.gitleaksignore` to reference the new line number:
```
README.md:curl-auth-header:344
```
Or use gitleaks v8.24.0+ fingerprint-only ignore format (omit line number).

**Affected files:** `.gitleaksignore`, `README.md` (line shift introduced in PR #309).

---

### D. Droid Auto Review — PRs #304–#311

**Error:** `curl -fsSL https://app.factory.ai/cli | sh` exits with code 1.

**Root Cause:** The Factory AI CLI installation or invocation is failing. This is an **infrastructure/service dependency issue**, not a code quality issue. The droid-review GitHub Action installs the Factory CLI and invokes the `review` skill, but the installation or API call fails.

All affected PRs show identical failure: the `curl | sh` step completes but the subsequent droid execution exits with code 1. The PR comment says "Droid encountered an error."

**Investigation needed:** This may be a transient Factory AI outage, rate limiting, or API auth issue. Not actionable from the GroktoCrawl codebase side. Check Factory AI service status or the droid-review action configuration.

**Affected:** GitHub Action `Factory-AI/droid-action` — not a codebase issue.

---

## 3. Failure Category Summary

| Category | PRs Affected | Root Cause Type | Fix Complexity |
|----------|-------------|-----------------|----------------|
| Integration Tests | #292–#311 (20 PRs) | CI config bug (wrong port) | **Trivial** — 1-line port fix |
| Dead Code (vulture) | #306–#311 (6 PRs) | Unused deprecated parameter | **Simple** — remove or prefix parameter |
| Gitleaks | #309–#311 (3 PRs) | Stale ignore line number | **Simple** — update line number |
| Droid Review | #304–#311 (8 PRs) | Infrastructure/3rd-party | **None** — not a code issue |

---

## 4. Recommended Fix Actions (Minimal PRs)

### Fix 1: CI Port Fix (unblocks ALL PRs)
**What:** Edit `.github/workflows/docker.yml` — change `localhost:8000/health` to `localhost:8005/health` in the "Wait for test-site" step.
**Target:** Merge directly to `main` or cherry-pick to earliest failing PR.
**Effect:** Resolves integration test failures for all 20 PRs.

### Fix 2: Dead Code Fix (unblocks PRs #306–#311)
**What:** In `agent-svc/agent/webhook.py`, remove the unused `webhook_id_key` parameter from `deliver_webhook()` (line 59). Alternatively, rename to `_webhook_id_key`.
**Target:** Apply to PR #306, then rebase #307–#311.
**Effect:** Resolves dead-code failure for 6 PRs.

### Fix 3: Gitleaks Ignore Update (unblocks PRs #309–#311)
**What:** Update `.gitleaksignore` to `README.md:curl-auth-header:344`.
**Target:** Apply to PR #309, then rebase #310–#311.
**Effect:** Resolves gitleaks failure for 3 PRs.

### Fix 4: Droid Review Investigation (separate action)
**What:** Check Factory AI service status; verify `FACTORY_API_KEY` secret is valid; check rate limits.
**Effect:** Resolves droid-review failures for PRs #304–#311.

---

## 5. PRs That Can Be Merged As-Is (All Green)

**None.** All 20 non-release PRs have at least the integration test failure.

After Fix 1 (CI port fix) is applied:
- PRs **#292–#295** and **#297** could become all-green (only integration tests failing, no droid-review or code quality issues). Note: droid-review is SKIPPED on these, not FAILED.

After Fix 1 + Fix 2 + Fix 3:
- All PRs would be green on code quality checks
- Droid-review remains a question mark (infrastructure dependency)

---

## 6. Dependency Order (Which PRs Block Others)

```
main
 ├── #293 (crawl-engine-core)                    ← base of crawl stack
 │    └── #294 (path-filtering)
 │         └── #295 (status-response-enhancement)
 │              └── #296 (crawl-metrics)
 │                   └── #297 (crawl-cli-update)
 │                        └── #298 (sitemap-parser)
 │                             └── #299 (domain-scope-controls)
 │                                  └── #300 (politeness-integration)
 │                                       └── #301 (content-dedup)
 │                                            └── #302 (crawl-cache)
 │                                                 └── #303 (crawl-active-endpoint)
 │                                                      └── #304 (fix-crawl-cache-combined-semantics)
 │                                                           └── #305 (nl-to-params)
 │                                                                └── #306 (per-page-webhooks)     ← dead-code introduced here
 │                                                                     └── #307 (sse-streaming)
 │                                                                          └── #308 (advanced-scrape-options)
 │                                                                               └── #309 (integration-tests)  ← gitleaks introduced here
 │                                                                                    └── #310 (fix-sse-error-events)
 │                                                                                         └── #311 (fix-docker-skips)
 ├── #292 (refactor-llmstxt)                    ← standalone, no blockers
 └── #291 (release-please)                       ← release bot, auto-generated
```

**Merge order:** #293 → #294 → ... → #311 (linear chain). Each PR depends on its parent. #292 can be merged independently once CI is green.

---

## 7. Actions Taken During Investigation

1. **Listed all 21 open PRs** with `gh pr list --json` — parsed status checks for each.
2. **Identified 4 failure categories** — integration tests (20 PRs), dead-code (6), gitleaks (3), droid-review (8).
3. **Investigated integration test logs** — found `RuntimeError: Timed out waiting for test-site` across all PRs, traced to port mismatch (8000 vs 8005 in docker-compose).
4. **Investigated dead-code logs** — found vulture flagging `webhook_id_key` at `webhook.py:59`, traced to PR #306.
5. **Investigated gitleaks logs** — found `curl-auth-header` false positive in README, traced to stale `.gitleaksignore` line number.
6. **Investigated droid-review logs** — found Factory AI CLI install failure, determined it's infrastructure-level.
7. **Checked PR review comments** — no human reviews exist; droid-review posted "Droid encountered an error" on PR #311.
8. **Mapped dependency chain** — verified stacked branch commit counts confirm linear relationship.

---

## 8. Immediate Action Plan

1. **Apply Fix 1 (CI port)** to main — `s/localhost:8000/localhost:8005/` in docker.yml "Wait for test-site" step.
2. **Apply Fix 2 (dead code)** to PR #306 — remove `webhook_id_key` parameter, then rebase #307–#311.
3. **Apply Fix 3 (gitleaks)** to PR #309 — update `.gitleaksignore` line number, then rebase #310–#311.
4. **Investigate Fix 4** — verify Factory AI service availability.
5. **Re-run CI** on all PRs after fixes land.
6. **Merge ready PRs** in dependency order starting from bottom of stack.
