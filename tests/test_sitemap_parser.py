"""Unit tests for SitemapParser in agent-svc/agent/sitemap_parser.py.

Covers:
- Basic XML sitemap parsing (<urlset>)
- Sitemap index file parsing (<sitemapindex>) with recursion
- Gzipped sitemap decompression
- Malformed XML handling
- 404 / 5xx sitemap URL handling
- Connection error handling
- Common path fallback (robots.txt → /sitemap.xml)
- robots.txt Sitemap directive discovery
- Text sitemap detection (unsupported)
- Empty sitemap handling
- Non-HTTP URL filtering
- get_urls with limit enforcement
- Nested sitemap index depth limit
"""

from __future__ import annotations

import gzip
from unittest.mock import AsyncMock

import httpx
import pytest
from agent.sitemap_parser import SitemapParser

# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def xml_sitemap() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/</loc></url>
  <url><loc>https://example.com/about</loc></url>
  <url><loc>https://example.com/pricing</loc></url>
  <url><loc>https://example.com/blog/post-1</loc></url>
  <url><loc>https://example.com/contact</loc></url>
</urlset>"""


@pytest.fixture
def sitemap_index() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap2.xml</loc></sitemap>
</sitemapindex>"""


@pytest.fixture
def child_sitemap1() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/page1</loc></url>
  <url><loc>https://example.com/page2</loc></url>
</urlset>"""


@pytest.fixture
def child_sitemap2() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/page3</loc></url>
  <url><loc>https://example.com/page4</loc></url>
</urlset>"""


@pytest.fixture
def empty_sitemap() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
</urlset>"""


def _mock_response(
    content: bytes | str,
    status_code: int = 200,
    content_type: str = "application/xml",
    content_encoding: str | None = None,
) -> AsyncMock:
    """Create a mock httpx.Response."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    resp = AsyncMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.content = content
    resp.headers = {"content-type": content_type}
    if content_encoding:
        resp.headers["content-encoding"] = content_encoding
    return resp


def _mock_gzipped_response(
    xml: str,
    status_code: int = 200,
    content_type: str = "application/xml",
) -> AsyncMock:
    """Create a mock gzipped httpx.Response."""
    compressed = gzip.compress(xml.encode("utf-8"))
    return _mock_response(
        compressed,
        status_code=status_code,
        content_type=content_type,
        content_encoding="gzip",
    )


# ── Basic URL set parsing tests ─────────────────────────────────


class TestParseUrlSet:
    """Tests for parsing a basic <urlset> sitemap."""

    @pytest.mark.asyncio
    async def test_parse_basic_sitemap(self, xml_sitemap):
        """A standard <urlset> sitemap is parsed correctly."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _mock_response(xml_sitemap)

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.xml")

        assert len(urls) == 5
        assert "https://example.com/" in urls
        assert "https://example.com/about" in urls
        assert "https://example.com/pricing" in urls
        assert "https://example.com/blog/post-1" in urls
        assert "https://example.com/contact" in urls

    @pytest.mark.asyncio
    async def test_parse_empty_sitemap(self, empty_sitemap):
        """An empty <urlset> returns an empty list."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _mock_response(empty_sitemap)

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.xml")

        assert urls == []

    @pytest.mark.asyncio
    async def test_parse_malformed_xml(self):
        """Malformed XML logs a warning and returns empty list."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _mock_response("This is not XML at all")

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.xml")

        assert urls == []

    @pytest.mark.asyncio
    async def test_parse_non_http_urls_filtered(self):
        """Non-HTTP URLs in sitemaps are filtered out."""
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/page1</loc></url>
  <url><loc>ftp://files.example.com/file</loc></url>
  <url><loc>mailto:admin@example.com</loc></url>
  <url><loc>https://example.com/page2</loc></url>
</urlset>"""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _mock_response(xml)

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.xml")

        assert len(urls) == 2
        assert "https://example.com/page1" in urls
        assert "https://example.com/page2" in urls
        assert "ftp://files.example.com/file" not in urls
        assert "mailto:admin@example.com" not in urls


# ── Sitemap index parsing tests ─────────────────────────────────


class TestSitemapIndex:
    """Tests for recursive sitemap index parsing."""

    @pytest.mark.asyncio
    async def test_parse_sitemap_index(
        self, sitemap_index, child_sitemap1, child_sitemap2
    ):
        """A sitemap index file is recursively parsed."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "sitemap_index.xml" in url:
                return _mock_response(sitemap_index)
            elif "sitemap1.xml" in url:
                return _mock_response(child_sitemap1)
            elif "sitemap2.xml" in url:
                return _mock_response(child_sitemap2)
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap_index.xml")

        assert len(urls) == 4
        assert "https://example.com/page1" in urls
        assert "https://example.com/page2" in urls
        assert "https://example.com/page3" in urls
        assert "https://example.com/page4" in urls

    @pytest.mark.asyncio
    async def test_sitemap_index_depth_limit(self, child_sitemap1, child_sitemap2):
        """Deeply nested sitemap indexes are limited by max_recursion_depth."""
        # Create index files that reference each other circularly
        nested_index1 = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/nested2.xml</loc></sitemap>
</sitemapindex>"""
        nested_index2 = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
</sitemapindex>"""

        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "nested1.xml" in url:
                return _mock_response(nested_index1)
            elif "nested2.xml" in url:
                return _mock_response(nested_index2)
            elif "sitemap1.xml" in url:
                return _mock_response(child_sitemap1)
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(
            client=mock_client,
            max_recursion_depth=1,
        )
        urls = await parser.parse_sitemap_url("https://example.com/nested1.xml")

        # With max_recursion_depth=1, nested2.xml should be allowed
        # but sitemap1.xml from nested2.xml should exceed depth limit
        # Wait: depth starts at 0. When parsing nested1.xml (depth=0),
        # it finds nested2.xml. We call _parse_sitemap(nested2.xml, depth=1).
        # That's depth=1, which is <= max_recursion_depth=1.
        # Then nested2.xml finds sitemap1.xml. We call _parse_sitemap(sitemap1.xml, depth=2).
        # depth=2 > max_recursion_depth=1, so sitemap1.xml is NOT parsed.
        # So urls should be empty.
        assert len(urls) == 0

    @pytest.mark.asyncio
    async def test_sitemap_index_deduplication(self, child_sitemap1):
        """Same child sitemap referenced twice is only parsed once."""
        sitemap_index_dup = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
  <sitemap><loc>https://example.com/sitemap1.xml</loc></sitemap>
</sitemapindex>"""

        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "sitemap_index_dup.xml" in url:
                return _mock_response(sitemap_index_dup)
            elif "sitemap1.xml" in url:
                return _mock_response(child_sitemap1)
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url(
            "https://example.com/sitemap_index_dup.xml"
        )

        assert len(urls) == 2
        assert "https://example.com/page1" in urls
        assert "https://example.com/page2" in urls


# ── Gzip tests ──────────────────────────────────────────────────


class TestGzipSitemaps:
    """Tests for gzipped sitemaps."""

    @pytest.mark.asyncio
    async def test_gzip_content_encoding(self, xml_sitemap):
        """Sitemaps with Content-Encoding: gzip are decompressed."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _mock_gzipped_response(xml_sitemap)

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.xml")

        assert len(urls) == 5
        assert "https://example.com/about" in urls

    @pytest.mark.asyncio
    async def test_gzip_with_gz_extension(self, xml_sitemap):
        """Sitemaps with .xml.gz extension are decompressed."""
        compressed = gzip.compress(xml_sitemap.encode("utf-8"))
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _mock_response(
            compressed,
            content_type="application/gzip",
        )

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.xml.gz")

        assert len(urls) == 5
        assert "https://example.com/pricing" in urls


# ── Error handling tests ────────────────────────────────────────


class TestErrorHandling:
    """Tests for graceful error handling."""

    @pytest.mark.asyncio
    async def test_404_returns_empty(self):
        """A sitemap returning 404 returns an empty list."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _mock_response("Not Found", status_code=404)

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.xml")

        assert urls == []

    @pytest.mark.asyncio
    async def test_500_returns_empty(self):
        """A sitemap returning 500 returns an empty list."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _mock_response("Server Error", status_code=500)

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.xml")

        assert urls == []

    @pytest.mark.asyncio
    async def test_connection_error_returns_empty(self):
        """A connection error when fetching sitemap returns empty."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = httpx.ConnectError("Connection refused")

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.xml")

        assert urls == []

    @pytest.mark.asyncio
    async def test_timeout_returns_empty(self):
        """A timeout fetching sitemap returns empty."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.side_effect = httpx.TimeoutException("Timeout after 15s")

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.xml")

        assert urls == []

    @pytest.mark.asyncio
    async def test_text_sitemap_unsupported(self):
        """A plain text sitemap is detected and returns empty."""
        text_sitemap = "https://example.com/page1\nhttps://example.com/page2\n"
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get.return_value = _mock_response(
            text_sitemap,
            content_type="text/plain",
        )

        parser = SitemapParser(client=mock_client)
        urls = await parser.parse_sitemap_url("https://example.com/sitemap.txt")

        assert urls == []


# ── get_urls integration tests ──────────────────────────────────


class TestGetUrls:
    """Tests for the full get_urls() flow."""

    @pytest.mark.asyncio
    async def test_get_urls_from_robots_txt(self, xml_sitemap):
        """get_urls discovers sitemaps from robots.txt first."""
        robots_txt = "User-agent: *\nSitemap: https://example.com/sitemap.xml\n"
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "robots.txt" in url:
                return _mock_response(robots_txt)
            elif "sitemap.xml" in url:
                return _mock_response(xml_sitemap)
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.get_urls("example.com")

        assert len(urls) == 5
        assert "https://example.com/" in urls
        assert "https://example.com/about" in urls

    @pytest.mark.asyncio
    async def test_get_urls_fallback_to_common_paths(self, xml_sitemap):
        """get_urls falls back to common paths when robots.txt has no sitemap directives."""
        robots_txt = "User-agent: *\nDisallow: /admin/\n"
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "robots.txt" in url:
                return _mock_response(robots_txt)
            elif "sitemap.xml" in url:
                return _mock_response(xml_sitemap)
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.get_urls("example.com")

        assert len(urls) == 5
        assert "https://example.com/" in urls

    @pytest.mark.asyncio
    async def test_get_urls_robots_txt_404_falls_back(self, xml_sitemap):
        """get_urls falls back to common paths when robots.txt is 404."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "robots.txt" in url:
                return _mock_response("Not Found", status_code=404)
            elif "sitemap.xml" in url:
                return _mock_response(xml_sitemap)
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.get_urls("example.com")

        assert len(urls) == 5

    @pytest.mark.asyncio
    async def test_get_urls_with_limit(self, xml_sitemap):
        """get_urls respects the limit parameter."""
        robots_txt = "User-agent: *\nSitemap: https://example.com/sitemap.xml\n"
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "robots.txt" in url:
                return _mock_response(robots_txt)
            elif "sitemap.xml" in url:
                return _mock_response(xml_sitemap)
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.get_urls("example.com", limit=3)

        assert len(urls) == 3

    @pytest.mark.asyncio
    async def test_get_urls_no_sitemap_returns_empty(self):
        """get_urls returns empty list when no sitemaps are found."""
        robots_txt = "User-agent: *\nDisallow: /\n"
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "robots.txt" in url:
                return _mock_response(robots_txt)
            # Simulate 404 on all common paths
            return _mock_response("Not Found", status_code=404)

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.get_urls("example.com")

        assert urls == []

    @pytest.mark.asyncio
    async def test_get_urls_deduplicates(self, xml_sitemap):
        """Duplicate URLs across sitemaps are deduplicated."""
        robots_txt = (
            "User-agent: *\n"
            "Sitemap: https://example.com/sitemap.xml\n"
            "Sitemap: https://example.com/sitemap2.xml\n"
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "robots.txt" in url:
                return _mock_response(robots_txt)
            elif "sitemap.xml" in url:
                return _mock_response(xml_sitemap)
            elif "sitemap2.xml" in url:
                return _mock_response(xml_sitemap)  # Same URLs
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.get_urls("example.com")

        assert len(urls) == 5  # Deduplicated

    @pytest.mark.asyncio
    async def test_get_urls_robots_txt_unreachable_falls_back(self, xml_sitemap):
        """get_urls falls back to common paths when robots.txt is unreachable."""
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "robots.txt" in url:
                raise httpx.ConnectError("Connection refused")
            elif "sitemap.xml" in url:
                return _mock_response(xml_sitemap)
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.get_urls("example.com")

        assert len(urls) == 5

    @pytest.mark.asyncio
    async def test_multiple_robots_txt_sitemap_directives(
        self, xml_sitemap, child_sitemap1
    ):
        """Multiple Sitemap directives in robots.txt are all fetched."""
        robots_txt = (
            "User-agent: *\n"
            "Sitemap: https://example.com/sitemap.xml\n"
            "Sitemap: https://example.com/extra-sitemap.xml\n"
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "robots.txt" in url:
                return _mock_response(robots_txt)
            elif url.endswith("extra-sitemap.xml"):
                return _mock_response(child_sitemap1)
            elif url.endswith("sitemap.xml"):
                return _mock_response(xml_sitemap)
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.get_urls("example.com")

        # 5 from sitemap.xml + 2 from extra-sitemap.xml = 7
        assert len(urls) == 7

    @pytest.mark.asyncio
    async def test_get_urls_with_nested_index(
        self, sitemap_index, child_sitemap1, child_sitemap2
    ):
        """get_urls follows nested sitemap indexes."""
        robots_txt = "User-agent: *\nSitemap: https://example.com/sitemap_index.xml\n"
        mock_client = AsyncMock(spec=httpx.AsyncClient)

        async def mock_get(url: str, **kwargs) -> AsyncMock:
            if "robots.txt" in url:
                return _mock_response(robots_txt)
            elif "sitemap_index.xml" in url:
                return _mock_response(sitemap_index)
            elif "sitemap1.xml" in url:
                return _mock_response(child_sitemap1)
            elif "sitemap2.xml" in url:
                return _mock_response(child_sitemap2)
            return _mock_response("")

        mock_client.get = AsyncMock(side_effect=mock_get)

        parser = SitemapParser(client=mock_client)
        urls = await parser.get_urls("example.com")

        assert len(urls) == 4
        assert "https://example.com/page1" in urls
        assert "https://example.com/page2" in urls
        assert "https://example.com/page3" in urls
        assert "https://example.com/page4" in urls


# ── URL normalization tests ─────────────────────────────────────


class TestUrlNormalization:
    """Tests for sitemap URL normalization."""

    def test_normalize_strips_fragment(self):
        """Fragments are stripped from sitemap URLs."""
        result = SitemapParser._normalize_sitemap_url(
            "https://example.com/page#section"
        )
        # Fragment is stripped by normalization
        assert result == "https://example.com/page"

    def test_normalize_rejects_non_http(self):
        """Non-HTTP URLs return None."""
        assert SitemapParser._normalize_sitemap_url("ftp://example.com/file") is None
        assert SitemapParser._normalize_sitemap_url("mailto:admin@example.com") is None
        assert SitemapParser._normalize_sitemap_url("") is None

    def test_normalize_preserves_https(self):
        """HTTPS URLs are preserved."""
        result = SitemapParser._normalize_sitemap_url("https://example.com/page")
        assert result == "https://example.com/page"
