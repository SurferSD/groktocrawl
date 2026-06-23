"""Unit tests for the shared LinkExtractor module.

Tests the ``extract_links()``, ``filter_links()``, and ``classify_links()``
functions from ``agent-svc/agent/link_extractor.py`` without needing a
running Docker stack.
"""

from __future__ import annotations

import os
import sys

# Add agent-svc to sys.path so we can import the agent package directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent-svc"))

from agent.link_extractor import (
    _is_subdomain,
    _strip_fragment,
    classify_links,
    extract_links,
    filter_links,
)

# ── extract_links() tests ──────────────────────────────────────────


class TestExtractLinks:
    """Tests for extract_links() — core link extraction."""

    def test_basic_link_extraction(self):
        """Should extract all <a href> links from simple HTML."""
        html = """
        <html>
        <body>
            <a href="/page1">Page 1</a>
            <a href="/page2">Page 2</a>
            <a href="/page3">Page 3</a>
        </body>
        </html>
        """
        links = extract_links(html, "https://example.com")
        assert len(links) == 3
        assert "https://example.com/page1" in links
        assert "https://example.com/page2" in links
        assert "https://example.com/page3" in links

    def test_absolute_links_preserved(self):
        """Should preserve already-absolute URLs unchanged."""
        html = '<a href="https://other.com/page">Link</a>'
        links = extract_links(html, "https://example.com")
        assert "https://other.com/page" in links

    def test_relative_urls_resolved(self):
        """Should resolve relative hrefs against base_url (VAL-CRAWL-035)."""
        html = '<a href="/about">About</a><a href="contact">Contact</a>'
        links = extract_links(html, "https://example.com/sub/")
        assert "https://example.com/about" in links
        assert "https://example.com/sub/contact" in links

    def test_fragment_stripped(self):
        """Should strip fragment identifiers from extracted URLs (VAL-CRAWL-036)."""
        html = '<a href="/about#team">Team</a><a href="/pricing#plans">Plans</a>'
        links = extract_links(html, "https://example.com")
        assert "https://example.com/about" in links
        assert "https://example.com/pricing" in links
        # No fragment-bearing URLs should be present
        for link in links:
            assert "#" not in link

    def test_duplicates_deduplicated(self):
        """Should deduplicate identical URLs (VAL-CRAWL-058)."""
        html = """
        <a href="/page">First</a>
        <a href="/page">Second</a>
        <a href="https://example.com/page">Third (absolute dup)</a>
        """
        links = extract_links(html, "https://example.com")
        assert len(links) == 1
        assert links == ["https://example.com/page"]

    def test_fragment_variants_deduplicated(self):
        """Fragment variants of same URL should resolve to same base URL."""
        html = '<a href="/about#team">Team</a><a href="/about#history">History</a>'
        links = extract_links(html, "https://example.com")
        assert len(links) == 1
        assert links == ["https://example.com/about"]

    def test_non_http_schemes_filtered(self):
        """Should filter out mailto:, tel:, javascript:, ftp: (VAL-CRAWL-057)."""
        html = """
        <a href="mailto:user@example.com">Email</a>
        <a href="tel:+1234567890">Call</a>
        <a href="javascript:void(0)">JS</a>
        <a href="ftp://files.example.com">FTP</a>
        <a href="/valid-page">Valid</a>
        """
        links = extract_links(html, "https://example.com")
        assert len(links) == 1
        assert links == ["https://example.com/valid-page"]

    def test_malformed_html_graceful(self):
        """Malformed HTML should not crash — graceful degradation (VAL-CRAWL-056)."""
        html = """<html><body><a href="/page1">Page1<a href="/page2">Page2"""
        links = extract_links(html, "https://example.com")
        assert len(links) >= 1

    def test_completely_garbled_html(self):
        """Completely garbled HTML should not crash."""
        html = "not even close to valid html <<<<>>>> ### %% ^^^^"
        links = extract_links(html, "https://example.com")
        assert isinstance(links, list)

    def test_empty_html(self):
        """Empty HTML should return empty list."""
        assert extract_links("", "https://example.com") == []

    def test_none_html(self):
        """None-like empty HTML should return empty list."""
        assert extract_links("", "https://example.com") == []

    def test_base_tag_respected(self):
        """<base> tag should override base_url for relative resolution (VAL-CRAWL-074)."""
        html = """
        <html>
        <head><base href="https://other.example.com/sub/"></head>
        <body>
            <a href="page">Relative to base</a>
            <a href="/root">Root-relative</a>
        </body>
        </html>
        """
        links = extract_links(html, "https://example.com")
        assert "https://other.example.com/sub/page" in links
        assert "https://other.example.com/root" in links

    def test_links_with_no_href_ignored(self):
        """<a> tags without href attribute should be ignored."""
        html = '<a>No href</a><a href="">Empty href</a><a href="/ok">OK</a>'
        links = extract_links(html, "https://example.com")
        assert links == ["https://example.com/ok"]

    def test_trailing_whitespace_in_href(self):
        """Href with leading/trailing whitespace should be trimmed."""
        html = '<a href="  /page  ">Spaced</a>'
        links = extract_links(html, "https://example.com")
        assert links == ["https://example.com/page"]

    def test_same_page_link_handled(self):
        """A link to '#' should resolve to the base URL without fragment."""
        html = '<a href="#">Top</a>'
        links = extract_links(html, "https://example.com")
        # urljoin("#", "https://example.com") returns "https://example.com"
        assert links == ["https://example.com"]

    def test_protocol_relative_url(self):
        """Protocol-relative URLs (//example.com/path) should resolve correctly."""
        html = '<a href="//cdn.example.com/file.js">CDN</a>'
        links = extract_links(html, "https://example.com")
        assert "https://cdn.example.com/file.js" in links

    def test_multiple_nested_a_tags(self):
        """Links nested in complex HTML structures should all be found."""
        html = """
        <div><ul><li><a href="/deep1">Deep</a></li></ul></div>
        <p><span><a href="/deep2">Nested</a></span></p>
        <a href="/top">Top</a>
        """
        links = extract_links(html, "https://example.com")
        assert len(links) == 3
        assert "https://example.com/deep1" in links
        assert "https://example.com/deep2" in links
        assert "https://example.com/top" in links

    def test_no_links_in_html(self):
        """HTML with no <a> tags should return empty list."""
        html = "<html><body><p>No links here</p></body></html>"
        assert extract_links(html, "https://example.com") == []


# ── filter_links() tests ────────────────────────────────────────────


class TestFilterLinks:
    """Tests for filter_links() — scope-based filtering."""

    def test_internal_links_included_by_default(self):
        """Internal (same-domain) links should always be included."""
        links = [
            "https://example.com/page1",
            "https://example.com/page2",
        ]
        filtered = filter_links(links, base_domain="example.com")
        assert len(filtered) == 2

    def test_subdomain_excluded_by_default(self):
        """Subdomain links should be excluded when allow_subdomains=False."""
        links = [
            "https://example.com/page",
            "https://docs.example.com/page",
        ]
        filtered = filter_links(links, base_domain="example.com")
        assert len(filtered) == 1
        assert "https://example.com/page" in filtered

    def test_subdomain_included_when_allowed(self):
        """Subdomain links should be included when allow_subdomains=True."""
        links = [
            "https://example.com/page",
            "https://docs.example.com/page",
            "https://blog.example.com/page",
        ]
        filtered = filter_links(links, base_domain="example.com", allow_subdomains=True)
        assert len(filtered) == 3
        assert "https://docs.example.com/page" in filtered

    def test_external_excluded_by_default(self):
        """External links should be excluded when allow_external_links=False."""
        links = [
            "https://example.com/page",
            "https://other.com/page",
        ]
        filtered = filter_links(links, base_domain="example.com")
        assert len(filtered) == 1
        assert "https://example.com/page" in filtered

    def test_external_included_when_allowed(self):
        """External links should be included when allow_external_links=True."""
        links = [
            "https://example.com/page",
            "https://other.com/page",
            "https://another.org/path",
        ]
        filtered = filter_links(
            links,
            base_domain="example.com",
            allow_external_links=True,
        )
        assert len(filtered) == 3
        assert "https://other.com/page" in filtered

    def test_subdomain_and_external_combined(self):
        """Both subdomain and external flags should work together."""
        links = [
            "https://example.com/page",
            "https://docs.example.com/page",
            "https://other.com/page",
        ]
        filtered = filter_links(
            links,
            base_domain="example.com",
            allow_subdomains=True,
            allow_external_links=True,
        )
        assert len(filtered) == 3

    def test_empty_links_list(self):
        """Empty links list should return empty list."""
        assert filter_links([], base_domain="example.com") == []

    def test_no_base_domain(self):
        """Without base_domain, all links should pass through."""
        links = ["https://example.com/page", "https://other.com/page"]
        filtered = filter_links(links)
        assert len(filtered) == 2

    def test_different_schemes_same_domain(self):
        """HTTP and HTTPS on same domain should both be internal."""
        links = [
            "http://example.com/page1",
            "https://example.com/page2",
        ]
        filtered = filter_links(links, base_domain="example.com")
        assert len(filtered) == 2

    def test_subdomain_with_port(self):
        """Subdomain with port should still be recognized as subdomain."""
        links = [
            "https://example.com/page",
            "https://docs.example.com:8080/page",
        ]
        # Without allow_subdomains
        filtered = filter_links(links, base_domain="example.com")
        assert len(filtered) == 1
        assert "https://example.com/page" in filtered

        # With allow_subdomains
        filtered = filter_links(links, base_domain="example.com", allow_subdomains=True)
        assert len(filtered) == 2

    def test_multi_level_subdomain(self):
        """Multi-level subdomains should be recognized."""
        links = ["https://deep.docs.example.com/page"]
        filtered = filter_links(links, base_domain="example.com")
        assert len(filtered) == 0

        filtered = filter_links(links, base_domain="example.com", allow_subdomains=True)
        assert len(filtered) == 1


# ── classify_links() tests ─────────────────────────────────────────


class TestClassifyLinks:
    """Tests for classify_links() — domain classification."""

    def test_classify_internal(self):
        """Same-domain URLs should be classified as internal."""
        links = ["https://example.com/page", "https://example.com/about"]
        result = classify_links(links, "https://example.com")
        assert "https://example.com/page" in result["internal"]
        assert "https://example.com/about" in result["internal"]
        assert result["subdomain"] == []
        assert result["external"] == []

    def test_classify_subdomain(self):
        """Subdomain URLs should be classified as subdomain."""
        links = ["https://docs.example.com/page", "https://blog.example.com/"]
        result = classify_links(links, "https://example.com")
        assert result["internal"] == []
        assert "https://docs.example.com/page" in result["subdomain"]
        assert "https://blog.example.com/" in result["subdomain"]
        assert result["external"] == []

    def test_classify_external(self):
        """Different-domain URLs should be classified as external."""
        links = ["https://other.com/page", "https://another.org/"]
        result = classify_links(links, "https://example.com")
        assert result["internal"] == []
        assert result["subdomain"] == []
        assert "https://other.com/page" in result["external"]

    def test_classify_mixed(self):
        """Mixed internal/subdomain/external URLs should be classified correctly."""
        links = [
            "https://example.com/internal",
            "https://docs.example.com/subdomain",
            "https://other.com/external",
        ]
        result = classify_links(links, "https://example.com")
        assert len(result["internal"]) == 1
        assert len(result["subdomain"]) == 1
        assert len(result["external"]) == 1

    def test_classify_with_port_difference(self):
        """Port difference does not change internal classification."""
        links = ["https://example.com:8080/page"]
        result = classify_links(links, "https://example.com")
        assert "https://example.com:8080/page" in result["internal"]

    def test_classify_with_path(self):
        """URLs with paths are still classified by domain."""
        links = ["https://example.com/sub/deep/page"]
        result = classify_links(links, "https://example.com")
        assert "https://example.com/sub/deep/page" in result["internal"]

    def test_classify_empty_links(self):
        """Empty links should return all-empty categories."""
        result = classify_links([], "https://example.com")
        assert result == {"internal": [], "subdomain": [], "external": []}


# ── Helper function tests ──────────────────────────────────────────


class TestStripFragment:
    """Tests for the _strip_fragment helper."""

    def test_strips_fragment(self):
        assert (
            _strip_fragment("https://example.com/page#section")
            == "https://example.com/page"
        )

    def test_no_fragment(self):
        assert _strip_fragment("https://example.com/page") == "https://example.com/page"

    def test_empty_url(self):
        assert _strip_fragment("") == ""

    def test_fragment_with_query(self):
        assert (
            _strip_fragment("https://example.com/page?a=1#section")
            == "https://example.com/page?a=1"
        )


class TestIsSubdomain:
    """Tests for the _is_subdomain helper."""

    def test_subdomain(self):
        assert _is_subdomain("docs.example.com", "example.com")

    def test_same_domain(self):
        assert not _is_subdomain("example.com", "example.com")

    def test_different_domain(self):
        assert not _is_subdomain("other.com", "example.com")

    def test_multi_level_subdomain(self):
        assert _is_subdomain("a.b.example.com", "example.com")

    def test_not_subdomain_with_prefix(self):
        """notexample.com is not a subdomain of example.com."""
        assert not _is_subdomain("notexample.com", "example.com")

    def test_case_insensitive(self):
        assert _is_subdomain("DOCS.EXAMPLE.COM", "example.com")
