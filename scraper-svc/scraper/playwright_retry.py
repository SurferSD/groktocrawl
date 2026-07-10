"""Bounded retry for known transient Playwright errors.

Provides a single async helper for retrying page.content() and
page.evaluate() calls that can fail transiently with "page is
navigating" or null-document.body "scrollHeight" errors.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_WAIT_SECONDS = 1.0


async def retry_transient(op, *args, **kwargs):
    """Call ``op(*args, **kwargs)`` with bounded retry on known transient errors.

    Catches only two Playwright transient signatures:
      - ``"page is navigating"``  (page.content() race)
      - ``"scrollheight"``        (null document.body during scrollTo)

    Re-raises *immediately* on non-matching exceptions.  Re-raises the
    original exception when all attempts are exhausted so that the outer
    ``fetch_via_playwright`` handler can classify it as ``browser_error``.
    """
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return await op(*args, **kwargs)
        except Exception as exc:
            if not _is_transient(str(exc)):
                raise
            if attempt < _MAX_ATTEMPTS - 1:
                logger.debug(
                    "Transient retry %d/%d: %s",
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    str(exc)[:120],
                )
                await asyncio.sleep(_WAIT_SECONDS)
                continue
            raise  # exhausted — let the outer handler classify


def _is_transient(msg: str) -> bool:
    """Return True when *msg* matches a known transient Playwright signature."""
    return "page is navigating" in msg or "scrollheight" in msg.lower()
