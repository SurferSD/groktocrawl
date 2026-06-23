"""Shared LinkExtractor module for extracting and filtering links from HTML.

Provides ``extract_links()``, ``filter_links()``, and ``classify_links()``
functions used by the crawl engine, ``/v2/map`` endpoint, and
``llmstxt.py``.

All functions are stateless — they accept inputs and return results
without any stored state.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


def extract_links(html: str, base_url: str) -> list[str]:
    """Extract and normalize all ``<a href>`` links from HTML.

    Args:
        html: Raw HTML content to parse.
        base_url: The page URL used to resolve relative links. A
            ``<base>`` tag in the HTML overrides this for relative
            resolution but not for domain classification.

    Returns:
        List of absolute, fragment-stripped, deduplicated URLs with
        ``http`` or ``https`` schemes. URLs are in discovery order
        (first occurrence wins for duplicates).

    Features:
        - Resolves relative URLs against ``base_url`` (or ``<base>`` tag
          if present in the HTML)
        - Strips fragment identifiers (``#section``)
        - Filters out non-http/https schemes (``mailto:``, ``tel:``,
          ``javascript:``, etc.)
        - Deduplicates URLs within the same page
        - Handles malformed HTML gracefully (returns empty list instead
          of raising exceptions)
    """
    if not html or not base_url:
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        logger.warning("Failed to parse HTML from %s", base_url)
        return []

    # Determine effective base URL for relative resolution.
    # <base> tag overrides the page URL.
    effective_base: str = base_url
    base_tag = soup.find("base", href=True)
    if base_tag is not None and hasattr(base_tag, "get"):
        href_val = base_tag.get("href")
        if href_val:
            effective_base = str(href_val)

    seen: set[str] = set()
    result: list[str] = []

    try:
        for a_tag in soup.find_all("a", href=True):
            if not hasattr(a_tag, "get"):
                continue
            href_val = a_tag.get("href")
            if href_val is None:
                continue
            href = _resolve_href_value(str(href_val))
            if not href:
                continue

            # Resolve relative URLs against the effective base
            try:
                full_url = urljoin(effective_base, href)
            except Exception:
                continue

            # Parse the resolved URL
            try:
                parsed = urlparse(full_url)
            except Exception:
                continue

            # Filter out non-http/https schemes
            scheme = parsed.scheme.lower()
            if scheme and scheme not in ("http", "https"):
                continue

            # Strip fragment identifier
            clean_url = _strip_fragment(full_url)

            # Deduplicate within this page
            if clean_url in seen:
                continue
            seen.add(clean_url)

            result.append(clean_url)

    except Exception:
        logger.warning("Error extracting links from %s", base_url, exc_info=True)

    return result


def filter_links(
    links: list[str],
    *,
    base_domain: str | None = None,
    allow_subdomains: bool = False,
    allow_external_links: bool = False,
) -> list[str]:
    """Filter a list of URLs by domain classification.

    Args:
        links: List of absolute URL strings (as returned by ``extract_links()``).
        base_domain: The hostname of the base domain (e.g., ``"example.com"``).
            If ``None``, only URLs that parse to a valid netloc are kept.
        allow_subdomains: If ``True``, URLs on subdomains of the base domain
            are included (e.g., ``docs.example.com`` when base is ``example.com``).
        allow_external_links: If ``True``, URLs on entirely different domains
            are included.

    Returns:
        Filtered list of URL strings matching the configured scope.
    """
    result: list[str] = []

    for url in links:
        parsed = urlparse(url)
        hostname = parsed.hostname.lower() if parsed.hostname else ""

        if not hostname:
            # Shouldn't happen with absolute URLs, but be safe
            result.append(url)
            continue

        if base_domain is None:
            result.append(url)
            continue

        base = base_domain.lower()

        if hostname == base:
            # Internal (same domain) — always included
            result.append(url)
        elif _is_subdomain(hostname, base) and allow_subdomains:
            result.append(url)
        elif (
            not _is_subdomain(hostname, base)
            and hostname != base
            and allow_external_links
        ):
            result.append(url)
        # subdomain without allow_subdomains → skip
        # external without allow_external_links → skip

    return result


def classify_links(links: list[str], base_url: str) -> dict[str, list[str]]:
    """Classify a list of URLs by domain relationship to the base URL.

    Args:
        links: List of absolute URL strings.
        base_url: The base URL used to determine the origin domain.

    Returns:
        Dict with keys ``"internal"``, ``"subdomain"``, and ``"external"``,
        each containing a list of URL strings.
    """
    parsed_base = urlparse(base_url)
    base_hostname = parsed_base.hostname.lower() if parsed_base.hostname else ""

    result: dict[str, list[str]] = {
        "internal": [],
        "subdomain": [],
        "external": [],
    }

    for url in links:
        parsed = urlparse(url)
        hostname = parsed.hostname.lower() if parsed.hostname else ""

        if not hostname:
            result["internal"].append(url)
        elif hostname == base_hostname:
            result["internal"].append(url)
        elif _is_subdomain(hostname, base_hostname):
            result["subdomain"].append(url)
        else:
            result["external"].append(url)

    return result


# ── Internal helpers ───────────────────────────────────────────────


def _strip_fragment(url: str) -> str:
    """Strip the fragment identifier from a URL, preserving the rest."""
    try:
        idx = url.index("#")
        return url[:idx]
    except ValueError:
        return url


def _is_subdomain(hostname: str, base: str) -> bool:
    """Check whether ``hostname`` is a subdomain of ``base``.

    ``blog.example.com`` is a subdomain of ``example.com``.
    ``example.com`` is NOT a subdomain of ``example.com``.
    ``other.com`` is NOT a subdomain of ``example.com``.
    Comparison is case-insensitive.
    """
    hostname_lower = hostname.lower()
    base_lower = base.lower()
    if hostname_lower == base_lower:
        return False
    return hostname_lower.endswith("." + base_lower)


def _resolve_href_value(href: str) -> str:
    """Normalize an href attribute value to a string."""
    return href.strip()
