"""Proxy configuration for HTTP and Playwright requests.

SCRAPER_PROXY_URL is an opt-in env var for residential/mobile IP rotation.
When set, httpx requests (Tiers 1-2) and Playwright browser contexts (Tier 3)
route through the proxy. Playwright uses context-level proxy assignment
(browser.new_context(proxy=...)) for job isolation.
If the proxy is unreachable, the scrape retries without proxy and logs a WARN.
Unset or empty = no proxy (default).
"""

import logging
from urllib.parse import urlparse

from .settings import load_settings

logger = logging.getLogger(__name__)

_settings = load_settings()
SCRAPER_PROXY_URL = _settings.scraper_proxy_url


def _get_httpx_proxies() -> str | None:
    """Get httpx-compatible proxy URL from env var.

    httpx 'proxy' parameter expects a single URL string.
    """
    return SCRAPER_PROXY_URL or None


def _get_playwright_proxy() -> dict | None:
    """Get Playwright-compatible proxy config from env var.

    Parses ************************** into Playwright's format.
    Handles URLs with no explicit port and IPv6 addresses (restores brackets).
    Uses context-level proxy (browser.new_context(proxy=...)) for job isolation.
    """
    if not SCRAPER_PROXY_URL:
        return None

    parsed = urlparse(SCRAPER_PROXY_URL)

    # Re-bracket IPv6 hostnames (urlparse strips them)
    hostname = parsed.hostname or "localhost"
    host = f"[{hostname}]" if ":" in hostname else hostname

    # Only include port when explicitly present
    if parsed.port is not None:
        server = f"{parsed.scheme}://{host}:{parsed.port}"
    else:
        server = f"{parsed.scheme}://{host}"

    config = {"server": server}
    if parsed.username:
        config["username"] = parsed.username
    if parsed.password:
        config["password"] = parsed.password
    return config


def _redact_proxy_url(url: str) -> str:
    """Redact password from a proxy URL for safe logging.

    Uses urlparse to extract the username, preserving the host:port structure
    while masking the password.
    """
    if not url:
        return url

    parsed = urlparse(url)
    if parsed.username:
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.username}:***@{hostname}{port}"
    return url
