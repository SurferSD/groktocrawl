"""Valkey-backed analytics counters.

Key scheme: ``analytics:counter:{name}``

Supports INCR and GET with TTL preservation. When a counter key already
has a TTL, incrementing it re-applies that TTL after the INCR operation
(since Valkey's INCR resets the key's TTL).

Usage::

    from redis import Redis
    from common.analytics import increment_counter, get_counter, get_all_counters

    redis = Redis.from_url("redis://localhost:6379/0", decode_responses=True)

    # Increment a counter
    increment_counter(redis, "page_views")

    # Read a counter
    value = get_counter(redis, "page_views")

    # Read all counters
    all_counters = get_all_counters(redis)
"""

import logging

logger = logging.getLogger(__name__)

COUNTER_KEY_PREFIX = "analytics:counter:"


def counter_key(name: str) -> str:
    """Return the full Valkey key for an analytics counter.

    Args:
        name: The counter name (e.g. ``"page_views"``).

    Returns:
        The key string ``analytics:counter:{name}``.
    """
    return f"{COUNTER_KEY_PREFIX}{name}"


def increment_counter(redis, name: str, ttl: int | None = None) -> int:
    """Increment an analytics counter and return the new value.

    Preserves any existing TTL on the key. If *ttl* is provided and the
    key did not already have a TTL, the key is set to expire after *ttl*
    seconds.

    Args:
        redis: A Valkey/Redis client (with ``incr``, ``ttl``, ``expire``).
        name: The counter name.
        ttl: Optional TTL in seconds to apply if the key has no TTL.

    Returns:
        The new counter value after the increment.
    """
    key = counter_key(name)
    # Preserve TTL: check existing TTL before INCR (INCR resets TTL)
    existing_ttl = redis.ttl(key)
    value = redis.incr(key)
    if existing_ttl is not None and existing_ttl > 0:
        redis.expire(key, existing_ttl)
    elif ttl is not None and (existing_ttl is None or existing_ttl < 0):
        redis.expire(key, ttl)
    return value


def get_counter(redis, name: str) -> int | None:
    """Get the current value of an analytics counter.

    Args:
        redis: A Valkey/Redis client.
        name: The counter name.

    Returns:
        The current value as an ``int``, or ``None`` if the counter does
        not exist.
    """
    key = counter_key(name)
    value = redis.get(key)
    if value is None:
        return None
    return int(value)


def get_all_counters(redis) -> dict[str, int]:
    """Get all analytics counters and their current values.

    Uses Valkey SCAN to discover ``analytics:counter:*`` keys. Returns
    a dict mapping counter names to their current integer values.

    Args:
        redis: A Valkey/Redis client (with ``scan`` and ``mget``).

    Returns:
        A dict like ``{"page_views": 42, "api_calls": 17}``.
    """
    cursor: int | str = 0
    counters: dict[str, int] = {}
    prefix_len = len(COUNTER_KEY_PREFIX)

    while True:
        cursor, keys = redis.scan(
            cursor=cursor, match=f"{COUNTER_KEY_PREFIX}*", count=100
        )  # type: ignore[assignment]
        if keys:
            values = redis.mget(*keys)  # type: ignore[arg-type]
            for key, val in zip(keys, values, strict=False):
                if val is not None:
                    name = key[prefix_len:]
                    counters[name] = int(val)
        if cursor == 0:
            break

    return counters
