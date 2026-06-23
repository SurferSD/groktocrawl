"""Shared retry utility with exponential backoff.

Used by all services for resilience against transient failures
in external dependencies (Valkey, Qdrant, upstream HTTP services).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_with_backoff(  # noqa: UP047
    fn: Callable[..., Awaitable[T]],
    *args: Any,
    max_retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 10.0,
    retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    **kwargs: Any,
) -> T:
    """Call ``fn(*args, **kwargs)`` with exponential backoff on failure.

    Args:
        fn: Async callable to retry.
        max_retries: Maximum number of retry attempts.
        base_delay: Initial delay in seconds (doubles each retry).
        max_delay: Maximum delay cap in seconds.
        retryable_exceptions: Exception types that trigger a retry.

    Returns:
        The return value of ``fn``.

    Raises:
        The last exception if all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except retryable_exceptions as exc:
            last_exc = exc
            if attempt == max_retries:
                break
            delay = min(base_delay * (2**attempt), max_delay)
            logger.warning(
                "Retry %d/%d after %.1fs for %s: %s",
                attempt + 1,
                max_retries,
                delay,
                getattr(fn, "__name__", str(fn)),
                exc,
            )
            await asyncio.sleep(delay)
    raise last_exc  # type: ignore[misc]
