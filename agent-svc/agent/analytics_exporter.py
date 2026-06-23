"""Background task that exports Valkey analytics counters as Prometheus metrics.

The exporter runs as a fire-and-forget background task inside agent-svc.
It periodically scans Valkey for ``analytics:counter:*`` keys and registers
each one as a Prometheus COUNTER metric with the ``groktocrawl_analytics_``
prefix.

The task is long-lived and re-reads all counters on each tick so that new
counters added by any service are picked up automatically.

Usage::

    # In app factory:
    app.state.task_tracker.create_background_task(
        start_analytics_exporter(redis_url="redis://valkey:6379/0")
    )
"""

import asyncio
import logging

from redis import Redis

logger = logging.getLogger(__name__)

# How often (in seconds) to re-read Valkey counters and update Prometheus metrics.
_DEFAULT_INTERVAL = 15


async def start_analytics_exporter(
    redis_url: str = "redis://localhost:6379/0",
    interval: float = _DEFAULT_INTERVAL,
) -> None:
    """Run the analytics counter exporter loop.

    Iterates every *interval* seconds, reads all ``analytics:counter:*``
    keys from Valkey, and updates the corresponding Prometheus COUNTER
    metrics on the shared ``METRICS`` collector.

    Because Prometheus COUNTERs are monotonically increasing and Valkey
    counters only ever go up (via INCR), the absolute value is set on
    each tick via the counter's ``set()`` method.
    """
    from common.analytics import get_all_counters
    from common.metrics import METRICS

    redis = Redis.from_url(redis_url, decode_responses=True)

    try:
        while True:
            try:
                counters = get_all_counters(redis)
                for name, value in counters.items():
                    metric_name = f"groktocrawl_analytics_{name}"
                    METRICS.counter(
                        metric_name,
                        f"Analytics counter: {name}",
                    ).set(value=float(value))
            except Exception:
                logger.exception("Failed to export analytics counters from Valkey")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("Analytics counter exporter cancelled")
        raise
    finally:
        redis.close()
