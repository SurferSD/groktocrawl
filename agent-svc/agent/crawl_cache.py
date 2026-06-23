"""Valkey-backed response cache for crawled pages with maxAge/minAge semantics.

Cache keys are SHA-256 hashes of the URL. Each entry stores the full page
data dict (the raw scrape response from ``ScraperClient``) along with a
``cached_at`` timestamp and the ``ttl_ms`` value that was set when the entry
was created.

Key format: ``crawl:cache:{sha256(url)}``

Value format (JSON)::

    {
        "url": "https://example.com/page",
        "data": {"success": true, "data": {"markdown": "...", ...}},
        "cached_at": "2025-01-01T00:00:00+00:00",
        "ttl_ms": 3600000
    }

The ``CrawlCache`` is checked by the ``CrawlEngine`` before each page scrape.
When a cache hit occurs and the entry is within ``maxAge``, the scraper call
is skipped entirely, saving network latency. When ``minAge`` is set, the
cache operates in cache-only mode: a cache miss returns an error rather than
fetching fresh content.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime

from redis import Redis

logger = logging.getLogger(__name__)

# Key prefix for all crawl cache entries in Valkey.
_CACHE_KEY_PREFIX = "crawl:cache:"


class CrawlCache:
    """Valkey-backed response cache for crawled pages.

    Each entry is stored as JSON under ``crawl:cache:{sha256(url)}`` with a
    TTL equal to the ``maxAge`` value in seconds (minimum 1 second).

    Usage::

        cache = CrawlCache("redis://valkey:6379/0")

        # Store a page response
        cache.set("https://example.com", response_dict, ttl_ms=3600000)

        # Check cache before scraping
        use_cached, data, error = cache.check_cache(
            "https://example.com",
            max_age_ms=3600000,  # 1 hour
            min_age_ms=60000,    # 1 minute
        )
        if error:
            # Cache miss in minAge mode
            ...
        elif use_cached and data:
            # Use the cached data directly
            ...
        else:
            # Cache miss or stale — scrape fresh
            ...
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        self.redis = Redis.from_url(redis_url, decode_responses=True)

    def _cache_key(self, url: str) -> str:
        """Generate a Valkey key for a URL using its SHA-256 hash.

        Returns:
            A string like ``crawl:cache:{hex_sha256}``.
        """
        url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return f"{_CACHE_KEY_PREFIX}{url_hash}"

    def get(self, url: str) -> dict | None:
        """Get the cached entry for a URL.

        Returns the full cached entry dict (with ``url``, ``data``,
        ``cached_at``, ``ttl_ms`` keys) if found, or ``None`` if the
        URL is not cached.

        The caller is responsible for checking maxAge/minAge constraints
        using ``check_cache()`` or by inspecting the entry directly.
        """
        key = self._cache_key(url)
        raw = self.redis.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to decode cache entry for %s: %s", url, exc)
            return None

    def set(self, url: str, data: dict, ttl_ms: int) -> None:
        """Store page data in the cache.

        The entry's TTL is derived from ``ttl_ms`` (converted to seconds,
        minimum 1 second). This ensures that cache entries expire
        naturally via Valkey's key expiration mechanism.

        Args:
            url: The URL being cached.
            data: The page data dict to store (typically the full scrape
                response dict from ``ScraperClient``).
            ttl_ms: Time-to-live in milliseconds for this cache entry.
                This should typically be the ``maxAge`` value.
        """
        key = self._cache_key(url)
        entry = {
            "url": url,
            "data": data,
            "cached_at": datetime.now(UTC).isoformat(),
            "ttl_ms": ttl_ms,
        }
        # Valkey TTL is in seconds. Ensure at least 1 second.
        ttl_seconds = max(1, int(ttl_ms / 1000))
        self.redis.set(key, json.dumps(entry), ex=ttl_seconds)

    def get_age_ms(self, url: str) -> int | None:
        """Get the age of a cached entry in milliseconds.

        Returns the age in milliseconds (time elapsed since ``cached_at``),
        or ``None`` if the URL is not cached or the timestamp is invalid.
        """
        entry = self.get(url)
        if entry is None:
            return None
        cached_at_raw = entry.get("cached_at")
        if cached_at_raw is None:
            return None
        try:
            cached_dt = datetime.fromisoformat(cached_at_raw)
            now = datetime.now(UTC)
            elapsed = now - cached_dt
            return int(elapsed.total_seconds() * 1000)
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Failed to parse cached_at for %s: %s — %s", url, cached_at_raw, exc
            )
            return None

    def delete(self, url: str) -> None:
        """Delete a cache entry for a URL.

        Removes the key from Valkey immediately.
        """
        key = self._cache_key(url)
        self.redis.delete(key)

    def check_cache(
        self,
        url: str,
        max_age_ms: int | None = None,
        min_age_ms: int | None = None,
    ) -> tuple[bool, dict | None, str | None]:
        """Check the cache and determine whether to use cached data.

        This is the primary entry point for the crawl engine. It evaluates
        the cache state against the ``maxAge``/``minAge`` constraints and
        returns a decision tuple.

        Decision matrix::

            maxAge=0 or None  → always fresh (bypass cache)
            maxAge>0, no cache → cache miss (fresh scrape)
            maxAge>0, cache hit, age < maxAge → use cached
            maxAge>0, cache hit, age >= maxAge → stale (fresh scrape)
            minAge>0, no cache → cache miss (ERROR)
            minAge>0, cache hit → use cached (regardless of age)
            Both set, no cache → cache miss (ERROR)
            Both set, cache hit, age < maxAge → use cached
            Both set, cache hit, age >= maxAge → stale (fresh scrape)

        Args:
            url: The URL to check in the cache.
            max_age_ms: ``maxAge`` in milliseconds. ``0`` or ``None`` means
                bypass cache (always scrape fresh).
            min_age_ms: ``minAge`` in milliseconds. When set and the URL
                is not cached, an error is returned.

        Returns:
            A tuple ``(use_cached, cached_data, error_message)``:

            - ``use_cached`` (bool): ``True`` if the caller should use the
              cached data directly without scraping.
            - ``cached_data`` (dict | None): The cached page data dict (the
              value of the ``data`` key in the stored entry, which is the
              scrape response), or ``None`` if not cached.
            - ``error_message`` (str | None): An error message if the cache
              returns an error state (e.g., minAge cache miss), or ``None``.
        """
        # If neither maxAge nor minAge is set, bypass cache entirely
        if (max_age_ms is None or max_age_ms == 0) and (
            min_age_ms is None or min_age_ms == 0
        ):
            logger.debug(
                "Cache bypass for %s (maxAge=%s, minAge=%s)",
                url,
                max_age_ms,
                min_age_ms,
            )
            return False, None, None

        entry = self.get(url)

        # ── No cached entry ──────────────────────────────────────
        if entry is None:
            if min_age_ms is not None and min_age_ms > 0:
                # minAge mode: cache miss is an error (VAL-SCRAPE-029)
                err_msg = (
                    f"Cache miss for {url} — no cached content available "
                    f"(minAge={min_age_ms}ms)"
                )
                logger.debug("Cache MISS (minAge) for %s", url)
                return False, None, err_msg
            # Normal cache miss — caller should scrape fresh
            logger.debug("Cache MISS for %s", url)
            return False, None, None

        cached_data = entry.get("data")
        age_ms = self.get_age_ms(url)

        # ── minAge mode — serve cached (with freshness check against maxAge) ─
        if min_age_ms is not None and min_age_ms > 0:
            if cached_data is not None:
                # When both minAge and maxAge are set, check age against maxAge.
                # If cache is older than maxAge, treat as stale and trigger
                # a fresh scrape instead of returning stale data.
                if (
                    max_age_ms is not None
                    and max_age_ms > 0
                    and age_ms is not None
                    and age_ms >= max_age_ms
                ):
                    logger.debug(
                        "Cache STALE (minAge+maxAge) for %s (age=%dms, maxAge=%dms)",
                        url,
                        age_ms,
                        max_age_ms,
                    )
                    return False, cached_data, None
                logger.debug(
                    "Cache HIT (minAge) for %s (age=%dms, minAge=%dms)",
                    url,
                    age_ms or 0,
                    min_age_ms,
                )
                return True, cached_data, None
            # Entry exists but has no data — treat as miss
            err_msg = (
                f"Cache miss for {url} — cached entry has no data "
                f"(minAge={min_age_ms}ms)"
            )
            return False, None, err_msg

        # ── maxAge mode — check freshness ─────────────────────────
        # At this point, either maxAge or minAge is set (otherwise we
        # would have returned early above). If only minAge is set, the
        # minAge block above already handled it. If we reach here,
        # maxAge must be set and > 0.
        assert max_age_ms is not None and max_age_ms > 0  # nosec — type narrowing
        if age_ms is not None and age_ms < max_age_ms:
            logger.debug(
                "Cache HIT for %s (age=%dms, maxAge=%dms)",
                url,
                age_ms,
                max_age_ms,
            )
            return True, cached_data, None

        # Cache is stale — caller should scrape fresh.
        # We still return cached_data so the caller can use it as
        # a fallback if desired (e.g., serve stale during revalidation).
        logger.debug(
            "Cache STALE for %s (age=%dms, maxAge=%dms)",
            url,
            age_ms or -1,
            max_age_ms,
        )
        return False, cached_data, None
