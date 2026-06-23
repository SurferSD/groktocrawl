"""
Adapter framework for site-specific content handlers.

Defines the base contracts:
    - SiteAdapter: what each handler implements
    - AdapterResult: what handlers return
    - AdapterContext: what the framework provides to handlers
    - AdapterRegistry: dispatch logic
    - @adapter decorator: auto-registration
"""

from __future__ import annotations

import asyncio
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ── Error types ──────────────────────────────────────────────────


class AdapterError(Exception):
    """Raised when an adapter cannot extract content from a URL."""


class AdapterTimeoutError(AdapterError):
    """Raised when an adapter exceeds its configured timeout."""


# ── Result type ──────────────────────────────────────────────────


@dataclass
class AdapterResult:
    """Structured result from a site adapter.

    The framework auto-merges ``metadata`` into YAML frontmatter
    prepended to ``markdown`` before returning to the caller.
    """

    success: bool
    markdown: str
    metadata: dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    url: str = ""

    def with_frontmatter(self) -> str:
        """Return markdown with YAML frontmatter prepended.

        Only adds a frontmatter block when ``metadata`` is non-empty.
        """
        if not self.metadata:
            return self.markdown
        lines = ["---"]
        for k, v in self.metadata.items():
            lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        lines.append(self.markdown)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Return a dict compatible with ``smart_scrape()`` return values."""
        return {
            "markdown": self.with_frontmatter(),
            "metadata": self.metadata,
            "source": self.source,
            "url": self.url,
        }


# ── Context provided to handlers ─────────────────────────────────


@dataclass
class AdapterContext:
    """Resources the framework makes available to all adapters."""

    browser_svc_url: str = ""
    logger: logging.Logger = logger
    config: dict[str, str] = field(default_factory=dict)

    async def with_timeout(self, coro, timeout: float = 15.0):
        """Run a coroutine with a bounded timeout.

        Raises ``AdapterTimeoutError`` if the coroutine does not
        complete within *timeout* seconds.
        """
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except TimeoutError:
            raise AdapterTimeoutError(f"Timed out after {timeout}s") from None


# ── Adapter base class ───────────────────────────────────────────


class SiteAdapter(ABC):
    """Contract for all site-specific content handlers.

    Subclasses declare ``name`` and ``patterns`` and implement
    ``scrape()``.  Override ``can_handle()`` for a fast pre-check
    beyond regex matching.
    """

    #: Human-readable name, e.g. ``"youtube"``.
    name: str = ""

    #: URL regex patterns this adapter can handle.
    #: First pattern match in the priority-sorted registry wins.
    patterns: list[re.Pattern] = []

    #: Higher = preferred when multiple adapters match.  Default 100.
    priority: int = 100

    async def can_handle(self, url: str) -> bool:
        """Optional fast pre-check beyond pattern matching.

        Called after a pattern matches.  Return ``True`` if this
        adapter can actually process this URL.
        """
        return True

    @abstractmethod
    async def scrape(self, url: str, ctx: AdapterContext) -> AdapterResult:
        """Extract content from *url*.

        Implement your own fallback chain here.  Raise ``AdapterError``
        (or ``AdapterTimeoutError``) on total failure — the registry
        will fall through to the next adapter or the generic pipeline.
        """


# ── Registry ─────────────────────────────────────────────────────


class AdapterRegistry:
    """Holds all registered adapters and dispatches URLs.

    Usage (single pattern — call ``init()`` once at startup)::

        registry = AdapterRegistry()
        registry.load_all()
        result = await registry.dispatch(url, ctx)
    """

    def __init__(self):
        self._entries: list[SiteAdapter] = []

    def register(self, adapter: SiteAdapter) -> None:
        """Add a single adapter instance to the registry."""
        self._entries.append(adapter)
        # Keep sorted by descending priority so dispatch iterates
        # highest-priority first.
        self._entries.sort(key=lambda e: e.priority, reverse=True)

    def load_all(self) -> None:
        """Import all known adapter modules, triggering @adapter registration.

        Called once at application startup.  Scans the ``adapters``
        package for modules whose classes carry the ``_adapter_cls``
        marker set by the ``@adapter`` decorator.
        """
        import importlib
        import pkgutil

        import scraper.adapters as adapters_pkg

        for _finder, name, _ispkg in pkgutil.iter_modules(
            adapters_pkg.__path__, prefix="scraper.adapters."
        ):
            if name == "scraper.adapters.base":
                continue
            if name == "scraper.adapters._helpers":
                continue
            try:
                importlib.import_module(name)
                logger.debug("Loaded adapter module: %s", name)
            except Exception as exc:
                logger.warning("Failed to load adapter module %s: %s", name, exc)

        # Register all decorated adapter classes
        # Must happen after imports to ensure @adapter decorators have fired.
        global _registry_list
        for cls in _registry_list:
            instance = cls()
            self.register(instance)
            logger.debug("Registered adapter: %s", instance.name)
        _registry_list = []

    async def dispatch(self, url: str, ctx: AdapterContext) -> AdapterResult | None:
        """Try each matching adapter in priority order.

        Returns the first successful ``AdapterResult``, or ``None``
        if no adapter matched or all matching adapters failed.
        """
        for entry in self._entries:
            if not any(p.search(url) for p in entry.patterns):
                continue
            if not await entry.can_handle(url):
                continue
            logger.info("Adapter %s matched for %s", entry.name, url)
            try:
                return await entry.scrape(url, ctx)
            except AdapterError as exc:
                logger.info("Adapter %s failed for %s: %s", entry.name, url, exc)
                continue
        return None


# ── Singleton ────────────────────────────────────────────────────

_instance: AdapterRegistry | None = None


def get_registry() -> AdapterRegistry:
    """Return the application-wide ``AdapterRegistry`` singleton."""
    global _instance
    if _instance is None:
        _instance = AdapterRegistry()
    return _instance


# ── @adapter decorator ────────────────────────────────────────────


_registry_list: list[type[SiteAdapter]] = []


def adapter(cls):
    """Decorator that marks a ``SiteAdapter`` subclass for auto-registration.

    Usage::

        @adapter
        class YouTubeAdapter(SiteAdapter):
            ...

    The adapter is automatically registered when its module is
    imported via ``AdapterRegistry.load_all()``.
    """
    _registry_list.append(cls)
    return cls
