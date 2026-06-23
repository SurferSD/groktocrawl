"""Multi-layer deduplication for crawl pages.

Provides ``DedupManager`` which implements:

1. **URL normalization** (Layer 1) — handled by ``CrawlEngine.normalize_url()``,
   not by this module. The crawl engine's seen-set prevents re-scraping
   the same normalized URL.

2. **Canonical tag check** (Layer 2) — after scraping a page, extract the
   ``<link rel="canonical">`` tag. If it points to a URL that has already
   been scraped in this crawl run, skip the current page.

3. **Content hash dedup** (Layer 3) — SHA-256 hash of the extracted
   markdown. Two pages with byte-for-byte identical markdown content are
   treated as duplicates.

Canonical check *always runs before* content hash check, so that a page
that is both canonical-duplicate and content-identical reports the
canonical dedup reason.
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import urljoin, urlparse

logger = logging.getLogger(__name__)


# Regex patterns for extracting <link rel="canonical" href="...">
# Supports both attribute orders: rel first or href first.
_CANONICAL_RE = re.compile(
    r'<link\s+[^>]*?rel=["\']canonical["\'][^>]*?href=["\']([^"\']+)["\'][^>]*/?>',
    re.IGNORECASE,
)
_CANONICAL_RE_HREF_FIRST = re.compile(
    r'<link\s+[^>]*?href=["\']([^"\']+)["\'][^>]*?rel=["\']canonical["\'][^>]*/?>',
    re.IGNORECASE,
)


class DedupManager:
    """Multi-layer deduplication manager for crawl runs.

    Usage::

        dedup = DedupManager()
        # … scrape page at url, get html and markdown …
        canonical_dup = dedup.check_canonical(html, url)
        if canonical_dup:
            # Skip this page (it's a canonical duplicate)
            continue

        content_hash = dedup.compute_content_hash(markdown)
        if content_hash and dedup.is_duplicate_content(content_hash):
            # Skip this page (same content as another page)
            continue

        dedup.mark_scraped(url, content_hash=content_hash)
    """

    def __init__(self) -> None:
        # URLs that have been fully scraped (not just seen in the queue).
        self._scraped_urls: set[str] = set()
        # URL → canonical URL mapping for already-scraped pages.
        self._canonical_urls: dict[str, str] = {}
        # SHA-256 hex digests of markdown content seen so far.
        self._content_hashes: set[str] = set()

    # ── Public API ─────────────────────────────────────────────

    def mark_scraped(
        self,
        url: str,
        canonical_url: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        """Register a URL as successfully scraped.

        Must be called *after* the page has passed all dedup checks.

        Args:
            url: The scraped URL (normalized form).
            canonical_url: Optional canonical URL found on the page.
            content_hash: Optional SHA-256 hex digest of the markdown.
        """
        normalized = url.rstrip("/")
        self._scraped_urls.add(normalized)
        if canonical_url:
            resolved = self._resolve_url(url, canonical_url)
            if resolved:
                self._canonical_urls[normalized] = resolved.rstrip("/")
        if content_hash:
            self._content_hashes.add(content_hash)

    def check_canonical(self, html: str, current_url: str) -> str | None:
        """Check whether this page is a canonical duplicate of an already-scraped page.

        Extracts the ``<link rel="canonical">`` tag from *html* and
        resolves it against *current_url*. Returns the canonical URL if:

        - The canonical URL is on the same domain as *current_url*.
        - The canonical URL is **not** the same as *current_url* (self-referencing
          canonical is ignored).
        - The canonical URL (or the URL it resolves to via the canonical chain)
          has already been scraped in this crawl run.

        Returns ``None`` when:
        - No ``<link rel="canonical">`` tag is found.
        - The canonical is self-referencing.
        - The canonical points to a different domain (external canonical is ignored).
        - The canonical target has **not** been scraped yet.

        Args:
            html: The raw HTML of the scraped page.
            current_url: The URL of the scraped page.

        Returns:
            The canonical URL if it is a duplicate of an already-scraped URL,
            or ``None`` if no canonical dedup applies.
        """
        canonical_href = self._extract_canonical_href(html)
        if canonical_href is None:
            return None

        # Resolve relative URLs against the current page URL.
        resolved = urljoin(current_url, canonical_href)

        # Normalise trailing-slash differences.
        resolved_normalized = resolved.rstrip("/")
        current_normalized = current_url.rstrip("/")

        # Self-referencing canonical → ignore.
        if resolved_normalized == current_normalized:
            logger.debug(
                "Self-referencing canonical ignored: %s → %s",
                current_url,
                resolved,
            )
            return None

        # External domain → ignore.
        current_domain = (urlparse(current_url).hostname or "").lower()
        resolved_domain = (urlparse(resolved).hostname or "").lower()
        if current_domain != resolved_domain:
            logger.debug(
                "External-domain canonical ignored: %s → %s",
                current_url,
                resolved,
            )
            return None

        # Check if the resolved canonical URL (or its alias via another canonical)
        # has already been scraped.
        if self._is_scraped(resolved_normalized):
            logger.debug(
                "Canonical duplicate: %s → already scraped %s",
                current_url,
                resolved,
            )
            return resolved

        return None

    def compute_content_hash(self, markdown: str) -> str | None:
        """Compute the SHA-256 hex digest of *markdown*.

        Returns ``None`` when *markdown* is empty or whitespace-only
        (empty-markdown pages are never treated as duplicates).

        Args:
            markdown: The extracted markdown text.

        Returns:
            SHA-256 hex digest string, or ``None`` for empty markdown.
        """
        if not markdown or not markdown.strip():
            return None
        return hashlib.sha256(markdown.encode("utf-8")).hexdigest()

    def is_duplicate_content(self, content_hash: str) -> bool:
        """Check whether *content_hash* has already been seen.

        Args:
            content_hash: SHA-256 hex digest to check.

        Returns:
            ``True`` if this hash was registered by a previous ``mark_scraped()``.
        """
        return content_hash in self._content_hashes

    def is_scraped_url(self, url: str) -> bool:
        """Check whether *url* has already been scraped.

        Args:
            url: The URL to check (will be normalised internally).

        Returns:
            ``True`` if the URL was previously registered via ``mark_scraped()``.
        """
        return url.rstrip("/") in self._scraped_urls

    def get_canonical_for(self, url: str) -> str | None:
        """Return the canonical URL that *url* points to, if known.

        Args:
            url: The source URL.

        Returns:
            The resolved canonical URL, or ``None`` if no canonical was
            recorded for *url*.
        """
        return self._canonical_urls.get(url.rstrip("/"))

    # ── Internal helpers ───────────────────────────────────────

    @staticmethod
    def _extract_canonical_href(html: str) -> str | None:
        """Extract the ``href`` value from a ``<link rel="canonical">`` tag.

        Tries both attribute-order patterns: ``rel="canonical" href="..."``
        and ``href="..." rel="canonical"``.
        """
        match = _CANONICAL_RE.search(html)
        if match:
            return match.group(1)
        match = _CANONICAL_RE_HREF_FIRST.search(html)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _resolve_url(base: str, url: str) -> str:
        """Resolve a potentially-relative URL against a base URL."""
        return urljoin(base, url)

    def _is_scraped(self, url: str) -> bool:
        """Check if *url* (or a canonical alias of it) has been scraped.

        Checks both the direct URL and any canonical-chain resolution.
        """
        normalized = url.rstrip("/")
        if normalized in self._scraped_urls:
            return True

        # Check if any scraped URL declares this URL as its canonical.
        for scraped, canonical in self._canonical_urls.items():
            if canonical == normalized:
                # The canonical target itself was scraped as another page.
                # Check if that scraped URL is known.
                if scraped in self._scraped_urls:
                    return True

        return False
