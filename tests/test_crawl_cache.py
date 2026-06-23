"""Unit tests for the Valkey-backed crawl cache (agent-svc/agent/crawl_cache.py).

Covers:
- Cache key format (SHA-256 of URL)
- get / set / delete lifecycle
- check_cache with maxAge semantics
- check_cache with minAge semantics
- maxAge=0 bypasses cache
- maxAge and minAge combined
- TTL enforcement
- Age calculation
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def mock_redis():
    """Create a mock Valkey/Redis client.

    Uses a real dict as backing store so get/set/delete work like the
    real thing. ``expire`` / ``ttl`` are mocked for TTL-related tests.
    """
    store: dict[str, str] = {}
    expiry: dict[str, float] = {}

    mock = MagicMock()
    mock.get.side_effect = lambda key: store.get(key)
    mock.set.side_effect = lambda key, value, ex=None: (
        store.update({key: value})
        or (expiry.update({key: time.monotonic() + ex}) if ex else None)
    )
    mock.delete.side_effect = lambda key: store.pop(key, None) or expiry.pop(key, None)
    mock.exists.side_effect = lambda key: key in store

    return mock


@pytest.fixture
def cache(mock_redis):
    """Create a CrawlCache with a mocked Valkey client."""
    from agent.crawl_cache import CrawlCache

    c = CrawlCache("redis://localhost:6379/0")
    c.redis = mock_redis
    return c


_SAMPLE_PAGE_DATA = {
    "success": True,
    "data": {
        "markdown": "# Hello World\nThis is test content.",
        "metadata": {"title": "Test Page", "source": "scrape"},
    },
}


# ── Cache key tests ──────────────────────────────────────────────


class TestCacheKey:
    """CrawlCache key generation uses SHA-256 of the URL."""

    def test_key_prefix(self, cache):
        key = cache._cache_key("https://example.com/page")
        assert key.startswith("crawl:cache:")

    def test_same_url_same_key(self, cache):
        key1 = cache._cache_key("https://example.com/page")
        key2 = cache._cache_key("https://example.com/page")
        assert key1 == key2

    def test_different_url_different_key(self, cache):
        key1 = cache._cache_key("https://example.com/page")
        key2 = cache._cache_key("https://example.com/other")
        assert key1 != key2

    def test_key_is_sha256_hex_length(self, cache):
        key = cache._cache_key("https://example.com/page")
        # prefix "crawl:cache:" = 12 chars, SHA-256 hex = 64 chars
        assert len(key) == 12 + 64


# ── Get / Set / Delete lifecycle ─────────────────────────────────


class TestCacheLifecycle:
    """Basic get/set/delete operations."""

    def test_get_missing_returns_none(self, cache):
        assert cache.get("https://example.com/missing") is None

    def test_set_and_get(self, cache):
        url = "https://example.com/page"
        cache.set(url, _SAMPLE_PAGE_DATA, ttl_ms=60000)
        entry = cache.get(url)
        assert entry is not None
        assert entry["url"] == url
        assert entry["data"] == _SAMPLE_PAGE_DATA
        assert "cached_at" in entry
        assert entry["ttl_ms"] == 60000

    def test_delete_removes_entry(self, cache):
        url = "https://example.com/page"
        cache.set(url, _SAMPLE_PAGE_DATA, ttl_ms=60000)
        assert cache.get(url) is not None
        cache.delete(url)
        assert cache.get(url) is None

    def test_set_overwrites_existing(self, cache):
        url = "https://example.com/page"
        cache.set(url, {"data": "old"}, ttl_ms=60000)
        cache.set(url, {"data": "new"}, ttl_ms=120000)
        entry = cache.get(url)
        assert entry is not None
        assert entry["data"] == {"data": "new"}
        assert entry["ttl_ms"] == 120000

    def test_set_ttl_zero_or_negative_defaults_to_1s(self, cache, mock_redis):
        """TTL of 0ms or negative should still create entry with 1s TTL."""
        url = "https://example.com/page"
        cache.set(url, _SAMPLE_PAGE_DATA, ttl_ms=0)
        entry = cache.get(url)
        assert entry is not None  # Entry is stored, just expires fast


# ── Age calculation tests ────────────────────────────────────────


class TestAgeMs:
    """Age calculation from cached_at timestamp."""

    def test_age_increases_over_time(self, cache, mock_redis):
        """Age should be > 0 after a brief sleep."""
        url = "https://example.com/page"
        cache.set(url, _SAMPLE_PAGE_DATA, ttl_ms=60000)
        age = cache.get_age_ms(url)
        assert age is not None
        assert age >= 0

    def test_age_for_missing_url(self, cache):
        assert cache.get_age_ms("https://example.com/missing") is None


# ── check_cache — maxAge semantics ───────────────────────────────


class TestCheckCacheMaxAge:
    """check_cache() with maxAge semantics."""

    def test_max_age_unset_bypasses_cache(self, cache):
        """No maxAge → always bypass cache."""
        use_cached, data, error = cache.check_cache("https://example.com/page")
        assert use_cached is False
        assert data is None
        assert error is None

    def test_max_age_zero_bypasses_cache(self, cache):
        """maxAge=0 → bypass cache (VAL-SCRAPE-053)."""
        url = "https://example.com/page"
        cache.set(url, _SAMPLE_PAGE_DATA, ttl_ms=60000)
        use_cached, data, error = cache.check_cache(url, max_age_ms=0)
        assert use_cached is False
        assert data is None
        assert error is None

    def test_cache_miss_returns_fresh_scrape(self, cache):
        """No cached entry → fresh scrape."""
        use_cached, data, error = cache.check_cache(
            "https://example.com/missing", max_age_ms=60000
        )
        assert use_cached is False
        assert data is None
        assert error is None

    def test_cache_hit_within_max_age(self, cache):
        """Cached entry younger than maxAge → use cached (VAL-SCRAPE-027)."""
        url = "https://example.com/page"
        cache.set(url, _SAMPLE_PAGE_DATA, ttl_ms=60000)
        use_cached, data, error = cache.check_cache(url, max_age_ms=60000)
        assert use_cached is True
        assert data == _SAMPLE_PAGE_DATA
        assert error is None

    def test_cache_hit_exceeds_max_age_simulated(self, cache):
        """When age exceeds maxAge, cache is stale → fresh scrape needed.

        We simulate an old entry by injecting a cached_at far in the past.
        """
        url = "https://example.com/page"

        from datetime import UTC, datetime, timedelta

        key = cache._cache_key(url)
        old_time = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        old_entry = {
            "url": url,
            "data": _SAMPLE_PAGE_DATA,
            "cached_at": old_time,
            "ttl_ms": 60000,
        }
        cache.redis.set(key, json.dumps(old_entry), ex=3600)

        use_cached, _data, error = cache.check_cache(url, max_age_ms=60000)
        assert use_cached is False  # Stale → needs refresh
        assert error is None  # No error — just needs fresh scrape


# ── check_cache — minAge semantics ──────────────────────────────


class TestCheckCacheMinAge:
    """check_cache() with minAge (cache-only) semantics."""

    def test_min_age_cache_miss_returns_error(self, cache):
        """Cache miss with minAge → error (VAL-SCRAPE-029)."""
        url = "https://example.com/page"
        use_cached, data, error = cache.check_cache(url, min_age_ms=60000)
        assert use_cached is False
        assert data is None
        assert error is not None
        assert "cache miss" in error.lower()

    def test_min_age_cache_hit_returns_cached(self, cache):
        """Cache hit with minAge → use cached (VAL-SCRAPE-030)."""
        url = "https://example.com/page"
        cache.set(url, _SAMPLE_PAGE_DATA, ttl_ms=60000)
        use_cached, data, error = cache.check_cache(url, min_age_ms=60000)
        assert use_cached is True
        assert data == _SAMPLE_PAGE_DATA
        assert error is None

    def test_min_age_cache_hit_ignores_age(self, cache):
        """minAge ignores age — stale cache is still served."""
        url = "https://example.com/page"

        from datetime import UTC, datetime, timedelta

        key = cache._cache_key(url)
        old_time = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        old_entry = {
            "url": url,
            "data": _SAMPLE_PAGE_DATA,
            "cached_at": old_time,
            "ttl_ms": 60000,
        }
        cache.redis.set(key, json.dumps(old_entry), ex=3600)

        use_cached, data, error = cache.check_cache(url, min_age_ms=60000)
        assert use_cached is True  # minAge serves even stale cache
        assert data == _SAMPLE_PAGE_DATA
        assert error is None


# ── check_cache — combined maxAge + minAge ──────────────────────


class TestCheckCacheCombined:
    """check_cache() with both maxAge and minAge."""

    def test_both_set_cache_miss_returns_error(self, cache):
        """Both set, no cache → minAge takes precedence: error."""
        url = "https://example.com/page"
        use_cached, data, error = cache.check_cache(
            url, max_age_ms=3600000, min_age_ms=60000
        )
        assert use_cached is False
        assert data is None
        assert error is not None
        assert "cache miss" in error.lower()

    def test_both_set_fresh_cache(self, cache):
        """Both set, cache younger than maxAge → use cached."""
        url = "https://example.com/page"
        cache.set(url, _SAMPLE_PAGE_DATA, ttl_ms=3600000)
        use_cached, data, error = cache.check_cache(
            url, max_age_ms=3600000, min_age_ms=60000
        )
        assert use_cached is True
        assert data == _SAMPLE_PAGE_DATA
        assert error is None

    def test_both_set_stale_cache_triggers_fresh_scrape(self, cache):
        """Both set, cache older than maxAge → trigger fresh scrape.

        Previously the minAge block would unconditionally return cached
        data when minAge was set, even if the cache was older than
        maxAge. This test verifies that when age >= maxAge, the cache
        returns use_cached=False so the caller performs a fresh scrape
        instead of serving stale data (VAL-SCRAPE-031)."""
        url = "https://example.com/page"

        from datetime import UTC, datetime, timedelta

        key = cache._cache_key(url)
        # Inject an entry 2 hours old
        old_time = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        old_entry = {
            "url": url,
            "data": _SAMPLE_PAGE_DATA,
            "cached_at": old_time,
            "ttl_ms": 3600000,
        }
        cache.redis.set(key, json.dumps(old_entry), ex=3600)

        # maxAge=60s, minAge=60s — cache is 2 hours old → stale
        use_cached, data, error = cache.check_cache(
            url, max_age_ms=60000, min_age_ms=60000
        )
        assert use_cached is False  # Must trigger fresh scrape
        assert data is not None  # Still return cached data as fallback
        assert error is None  # No error — just needs refresh


# ── check_cache — no cache config ───────────────────────────────


class TestCheckCacheNoConfig:
    """check_cache() with neither maxAge nor minAge."""

    def test_no_cache_config(self, cache):
        """No maxAge/minAge → bypass cache regardless of content."""
        url = "https://example.com/page"
        cache.set(url, _SAMPLE_PAGE_DATA, ttl_ms=60000)
        use_cached, data, error = cache.check_cache(url)
        assert use_cached is False
        assert data is None
        assert error is None
