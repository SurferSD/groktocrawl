"""Sliding-window rate limiter using Valkey/Redis.

Usage::

    limiter = SlidingWindowRateLimiter(redis, limit=10, window_seconds=60)
    allowed, remaining = await limiter.check("client_ip:search")
"""

import logging
import time

logger = logging.getLogger(__name__)


class SlidingWindowRateLimiter:
    """Sliding-window rate limiter backed by Valkey INCR/EXPIRE.

    Tracks request counts per key within a fixed time window. Counts
    for the current window slot are maintained atomically via Redis
    INCR. The key expires after ``window * 2`` seconds to avoid
    lingering keys.

    The limiter **fails open** — if Redis is unreachable, ``check()``
    returns ``(True, self.limit)`` so that transient infrastructure
    issues do not block legitimate traffic.
    """

    def __init__(self, redis: object, limit: int, window_seconds: int) -> None:
        self.redis = redis
        self.limit = limit
        self.window = window_seconds

    async def check(self, key: str) -> tuple[bool, int]:
        """Check whether *key* is within the rate limit.

        Args:
            key: Unique identifier for the client (e.g. ``client_ip:search``).

        Returns:
            Tuple of ``(allowed, remaining)`` where *allowed* is
            ``True`` if the request is within the limit, and *remaining*
            is the number of requests still available in the current
            window.
        """
        now = int(time.time())
        window_key = f"rate_limit:search:{key}:{now // self.window}"
        try:
            count = self.redis.incr(window_key)  # type: ignore[attr-defined]
            if count == 1:
                self.redis.expire(window_key, self.window * 2)  # type: ignore[attr-defined]
            remaining = max(0, self.limit - count)
            return count <= self.limit, remaining
        except Exception as e:
            logger.warning("Rate limiter check failed: %s", e)
            return True, self.limit  # Fail open

    @staticmethod
    def parse_limit(limit_str: str) -> tuple[int, int]:
        """Parse a limit string like ``"10/60s"`` into ``(limit, window_seconds)``.

        Supports suffixes ``s`` (seconds). If no suffix is present,
        the value is treated as seconds.

        Raises:
            ValueError: If the string cannot be parsed.
        """
        parts = limit_str.split("/")
        if len(parts) != 2:
            raise ValueError(
                f"Invalid rate limit format: {limit_str!r} — expected 'count/window' (e.g. '10/60s')"
            )
        limit = int(parts[0])
        window_str = parts[1].strip()
        window_str = window_str.removesuffix("s")
        window = int(window_str)
        if limit <= 0 or window <= 0:
            raise ValueError(
                f"Rate limit values must be positive: limit={limit}, window={window}"
            )
        return limit, window
