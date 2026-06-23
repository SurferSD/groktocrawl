"""TaskTracker: graceful shutdown for fire-and-forget background tasks.

Replaces bare ``asyncio.create_task()`` with tracked tasks that are
automatically removed from tracking on completion. On shutdown,
any remaining tasks get a grace period to finish before cancellation.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)


class TaskTracker:
    """Tracks background tasks for graceful shutdown.

    Each task created via ``create_background_task`` is added to a
    tracking set and automatically removed when it completes. On
    ``shutdown()``, any remaining tasks get a configurable grace
    period before being cancelled and awaited.
    """

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._shutdown_event = asyncio.Event()

    def create_background_task(
        self, coro: Coroutine[Any, Any, Any]
    ) -> asyncio.Task[Any]:
        """Create, track, and return a background task.

        The task is automatically removed from tracking when it
        completes (whether successful, failed, or cancelled).
        """
        task: asyncio.Task[Any] = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    async def shutdown(self, grace_period: float = 5.0) -> None:
        """Signal shutdown, cancel tracked tasks after grace period.

        Best-effort: tasks get up to *grace_period* seconds to finish
        normally. After that, remaining tasks are cancelled and awaited
        with ``return_exceptions=True`` to avoid unhandled exceptions
        during shutdown.
        """
        self._shutdown_event.set()
        if not self._tasks:
            return

        logger.info(
            "Shutting down %d background tasks (grace=%ss)",
            len(self._tasks),
            grace_period,
        )

        _, pending = await asyncio.wait(self._tasks, timeout=grace_period)

        if pending:
            logger.warning(
                "Cancelling %d tasks after %ss grace period",
                len(pending),
                grace_period,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
