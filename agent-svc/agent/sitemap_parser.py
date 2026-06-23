"""SitemapParser — fetches and parses XML sitemaps for GroktoCrawl.

Discovers sitemap URLs from robots.txt ``Sitemap:`` directives and
common locations (``/sitemap.xml``, ``/sitemap_index.xml``). Parses
XML sitemaps (``<urlset>``) and sitemap index files (``<sitemapindex>``)
recursively, handles gzipped content, and degrades gracefully on errors.
"""

from __future__ import annotations

import gzip
import logging
from collections.abc import Sequence
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

# Default sitemap locations to try when robots.txt has no Sitemap directive.
_COMMON_SITEMAP_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
]

# Maximum recursion depth for sitemap index files.
_MAX_RECURSION_DEPTH = 3

# HTTP client timeout for sitemap fetches.
_REQUEST_TIMEOUT = 15.0


# ── Public API ───────────────────────────────────────────────────


class SitemapParser:
    """Fetch and parse XML sitemaps for a domain.

    Usage::

        parser = SitemapParser()
        urls = await parser.get_urls("example.com", limit=100)
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        common_sitemap_paths: Sequence[str] | None = None,
        max_recursion_depth: int = _MAX_RECURSION_DEPTH,
    ):
        """Initialize the parser.

        Args:
            client: An optional ``httpx.AsyncClient`` to reuse. If
                ``None``, a new client is created (and closed after
                ``get_urls`` returns).
            common_sitemap_paths: List of paths to check when robots.txt
                has no ``Sitemap:`` directive. Defaults to
                ``["/sitemap.xml", "/sitemap_index.xml"]``.
            max_recursion_depth: Maximum depth for following nested
                sitemap index files.
        """
        self._client = client
        self._common_paths = (
            list(common_sitemap_paths)
            if common_sitemap_paths is not None
            else list(_COMMON_SITEMAP_PATHS)
        )
        self._max_depth = max_recursion_depth
        self._owned_client = client is None

    async def get_urls(
        self,
        domain: str,
        limit: int | None = None,
    ) -> list[str]:
        """Get all URLs from sitemaps for the given domain.

        Discovers sitemap URLs from robots.txt (``Sitemap:`` directives)
        and falls back to common locations. Parses XML sitemaps and
        sitemap index files recursively.

        Args:
            domain: The domain to fetch sitemaps for (e.g.,
                ``"example.com"``). Should not include a scheme.
            limit: Maximum number of URLs to return. If ``None``, all
                discovered URLs are returned.

        Returns:
            A list of unique, absolute URLs from the sitemap(s).
        """
        client = await self._ensure_client()
        try:
            sitemap_urls = await self._discover_sitemap_urls(domain, client)
            if not sitemap_urls:
                logger.info("No sitemap URLs found for %s, trying common paths", domain)
                sitemap_urls = await self._try_common_paths(domain, client)

            if not sitemap_urls:
                logger.warning("No sitemap found for %s at any location", domain)
                return []

            # Parse all discovered sitemap URLs
            all_urls: list[str] = []
            seen: set[str] = set()

            for sitemap_url in sitemap_urls:
                if len(all_urls) >= (limit or float("inf")):
                    break
                parsed = await self._parse_sitemap(
                    sitemap_url, client, depth=0, seen=seen
                )
                for url in parsed:
                    if len(all_urls) >= (limit or float("inf")):
                        break
                    normalized = self._normalize_sitemap_url(url)
                    if normalized and normalized not in seen:
                        seen.add(normalized)
                        all_urls.append(normalized)

            return all_urls
        finally:
            if self._owned_client:
                await client.aclose()

    async def parse_sitemap_url(
        self, sitemap_url: str, domain: str | None = None
    ) -> list[str]:
        """Parse a single sitemap URL and return its URLs.

        This is useful when the caller already knows the sitemap URL
        (e.g., from a robots.txt directive parsed by the politeness
        module).

        Args:
            sitemap_url: The full URL of the sitemap to parse.
            domain: Optional domain hint for logging. If not provided,
                extracted from ``sitemap_url``.

        Returns:
            A list of unique, absolute URLs from the sitemap.
        """
        client = await self._ensure_client()
        try:
            seen: set[str] = set()
            urls = await self._parse_sitemap(sitemap_url, client, depth=0, seen=seen)
            result: list[str] = []
            for url in urls:
                normalized = self._normalize_sitemap_url(url)
                if normalized and normalized not in result:
                    result.append(normalized)
            return result
        finally:
            if self._owned_client:
                await client.aclose()

    # ── Internal helpers ─────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Get or create an HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=True, timeout=_REQUEST_TIMEOUT
            )
            self._owned_client = True
        return self._client

    async def _discover_sitemap_urls(
        self, domain: str, client: httpx.AsyncClient
    ) -> list[str]:
        """Discover sitemap URLs from robots.txt ``Sitemap:`` directives.

        Args:
            domain: The domain to check.
            client: The HTTP client to use.

        Returns:
            A list of sitemap URLs from robots.txt, or empty list if
            robots.txt has no ``Sitemap:`` directive or is unreachable.
        """
        robots_url = f"https://{domain}/robots.txt"
        try:
            resp = await client.get(robots_url)
            if resp.status_code != 200:
                logger.debug(
                    "robots.txt returned %d for %s, trying common paths",
                    resp.status_code,
                    domain,
                )
                return []
            # Decompress gzipped robots.txt if needed
            content = await self._decompress_content(resp)
            if not content:
                return []

            # Extract Sitemap directives
            sitemap_urls: list[str] = []
            for line in content.splitlines():
                if line.lower().startswith("sitemap:"):
                    sitemap_urls.append(line.split(":", 1)[1].strip())

            logger.info(
                "Found %d sitemap URL(s) in robots.txt for %s",
                len(sitemap_urls),
                domain,
            )
            return sitemap_urls
        except httpx.RequestError as exc:
            logger.warning("Failed to fetch robots.txt for %s: %s", domain, exc)
            return []
        except Exception as exc:
            logger.warning("Error parsing robots.txt for %s: %s", domain, exc)
            return []

    async def _try_common_paths(
        self, domain: str, client: httpx.AsyncClient
    ) -> list[str]:
        """Try common sitemap paths as a fallback.

        Args:
            domain: The domain to check.
            client: The HTTP client to use.

        Returns:
            A list of sitemap URLs that returned a successful response.
        """
        found: list[str] = []
        for path in self._common_paths:
            url = f"https://{domain}{path}"
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "").lower()
                    # Accept XML or text content, or gzipped content
                    if (
                        "xml" in content_type
                        or "text" in content_type
                        or "gzip" in content_type
                        or "octet-stream" in content_type
                        or path.endswith(".gz")
                        or not content_type
                    ):
                        logger.info("Found sitemap at %s", url)
                        found.append(url)
                    else:
                        logger.debug(
                            "Skipping %s (content-type: %s)", url, content_type
                        )
                else:
                    logger.debug(
                        "Common path %s returned %d for %s",
                        url,
                        resp.status_code,
                        domain,
                    )
            except httpx.RequestError as exc:
                logger.debug(
                    "Failed to fetch common path %s for %s: %s",
                    path,
                    domain,
                    exc,
                )
        return found

    async def _parse_sitemap(
        self,
        sitemap_url: str,
        client: httpx.AsyncClient,
        depth: int,
        seen: set[str],
    ) -> list[str]:
        """Parse a single sitemap URL and return discovered URLs.

        Handles both ``<urlset>`` (leaf sitemap) and ``<sitemapindex>``
        (index file) XML formats, as well as gzipped content and plain
        text sitemaps.

        Args:
            sitemap_url: The URL of the sitemap to fetch.
            client: The HTTP client.
            depth: Current recursion depth (for nested indexes).
            seen: Set of already-seen sitemap URLs to avoid re-parsing.

        Returns:
            A list of discovered page URLs.
        """
        if depth > self._max_depth:
            logger.warning("Max recursion depth reached for %s", sitemap_url)
            return []

        # Avoid re-parsing the same sitemap URL
        sitemap_normalized = self._normalize_sitemap_url(sitemap_url)
        if sitemap_normalized and sitemap_normalized in seen:
            logger.debug("Sitemap already parsed: %s", sitemap_url)
            return []
        if sitemap_normalized:
            seen.add(sitemap_normalized)

        # Fetch the sitemap
        try:
            resp = await client.get(sitemap_url)
        except httpx.RequestError as exc:
            logger.warning("Failed to fetch sitemap %s: %s", sitemap_url, exc)
            return []

        if resp.status_code >= 500:
            logger.warning(
                "Sitemap %s returned HTTP %d (server error)",
                sitemap_url,
                resp.status_code,
            )
            return []
        if resp.status_code == 404:
            logger.warning("Sitemap %s returned 404 (not found)", sitemap_url)
            return []

        if resp.status_code != 200:
            logger.warning(
                "Sitemap %s returned HTTP %d, skipping",
                sitemap_url,
                resp.status_code,
            )
            return []

        # Decompress if needed
        content = await self._decompress_content(resp)
        if not content:
            return []

        # Check if it's a plain text sitemap (not XML)
        is_text_sitemap = sitemap_url.endswith(".txt") or (
            not content.strip().startswith("<")
        )
        if is_text_sitemap and not content.strip().startswith("<"):
            logger.warning(
                "Text sitemap detected at %s (unsupported) — "
                "falling back to HTML-only discovery",
                sitemap_url,
            )
            return []

        return await self._parse_xml_content(content, sitemap_url, client, depth, seen)

    async def _parse_xml_content(
        self,
        content: str,
        sitemap_url: str,
        client: httpx.AsyncClient,
        depth: int,
        seen: set[str],
    ) -> list[str]:
        """Parse XML sitemap content and extract URLs.

        Handles both ``<urlset>`` (leaf sitemap) and ``<sitemapindex>``
        (index) XML formats.

        Args:
            content: The XML content as a string.
            sitemap_url: The sitemap URL (for logging).
            client: The HTTP client.
            depth: Current recursion depth.
            seen: Set of already-seen sitemap/sitemap URLs.

        Returns:
            A list of discovered page URLs.
        """
        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            logger.warning(
                "Malformed XML in sitemap %s: %s — falling back to HTML-only discovery",
                sitemap_url,
                exc,
            )
            return []

        # Determine the local tag name (handles namespace-prefixed tags)
        def _local_tag(element) -> str:
            tag = element.tag
            if "}" in tag:
                return tag.split("}", 1)[1]
            return tag

        root_tag = _local_tag(root)

        if root_tag == "sitemapindex":
            return await self._parse_sitemap_index(
                root, client, depth, seen, _local_tag
            )
        elif root_tag == "urlset":
            return self._parse_urlset(root, _local_tag)
        else:
            logger.warning(
                "Unknown sitemap root element <%s> in %s",
                root_tag,
                sitemap_url,
            )
            return []

    def _parse_urlset(self, root: ElementTree.Element, local_tag_fn) -> list[str]:
        """Extract URLs from a ``<urlset>`` element.

        Args:
            root: The ``<urlset>`` XML element.
            local_tag_fn: Function to get the local tag name.

        Returns:
            A list of ``<loc>`` values.
        """
        urls: list[str] = []
        for url_elem in root:
            for child in url_elem:
                if local_tag_fn(child) == "loc" and child.text:
                    url = child.text.strip()
                    if url.startswith(("http://", "https://")):
                        urls.append(url)
        return urls

    async def _parse_sitemap_index(
        self,
        root: ElementTree.Element,
        client: httpx.AsyncClient,
        depth: int,
        seen: set[str],
        local_tag_fn,
    ) -> list[str]:
        """Recursively parse a sitemap index file.

        Args:
            root: The ``<sitemapindex>`` XML element.
            client: The HTTP client.
            depth: Current recursion depth.
            seen: Set of already-seen sitemap/sitemap URLs.
            local_tag_fn: Function to get the local tag name.

        Returns:
            A list of aggregated page URLs from all child sitemaps.
        """
        child_sitemaps: list[str] = []
        for sitemap_elem in root:
            for child in sitemap_elem:
                if local_tag_fn(child) == "loc" and child.text:
                    child_sitemaps.append(child.text.strip())

        all_urls: list[str] = []
        for child_url in child_sitemaps:
            if depth + 1 > self._max_depth:
                logger.warning(
                    "Max recursion depth reached for child sitemap %s",
                    child_url,
                )
                continue
            child_urls = await self._parse_sitemap(child_url, client, depth + 1, seen)
            all_urls.extend(child_urls)
        return all_urls

    @staticmethod
    async def _decompress_content(resp: httpx.Response) -> str | None:
        """Decompress gzipped response content if needed.

        Handles ``Content-Encoding: gzip`` and ``.gz`` extension.

        Args:
            resp: The HTTP response.

        Returns:
            Decoded string content, or ``None`` if decompression fails.
        """
        content_encoding = resp.headers.get("content-encoding", "").lower()
        raw = resp.content

        if content_encoding == "gzip" or raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
            except (gzip.BadGzipFile, OSError) as exc:
                logger.warning("Failed to decompress gzip content: %s", exc)
                # Content may already be decompressed by httpx; use text directly
                return resp.text

        # Try UTF-8 first, then fall back to common encodings
        for encoding in ("utf-8", "utf-8-sig", "latin-1", "iso-8859-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue

        logger.warning("Could not decode sitemap content as text")
        return None

    @staticmethod
    def _normalize_sitemap_url(url: str) -> str | None:
        """Normalize a sitemap URL (strip fragments, basic cleanup).

        Args:
            url: The URL to normalize.

        Returns:
            Normalized URL, or ``None`` if the URL is not HTTP(S).
        """
        if not url or not url.startswith(("http://", "https://")):
            return None
        parsed = urlparse(url)
        # Strip fragment
        normalized = parsed._replace(fragment="").geturl()
        return normalized
