"""Unit tests for llmstxt.py pure functions.

Tests the _extract_description() and discover_pages() functions directly
without needing a running Docker stack.
Run with: python3 -m pytest tests/test_llmstxt_unit.py -v
"""

import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

# Add the agent-svc directory to the path so we can import llmstxt
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent-svc"))

from agent.llmstxt import _extract_description, discover_pages


def test_ends_at_sentence_boundary():
    """Description should end at a sentence boundary, not mid-word."""
    text = (
        "This is the first sentence of the page content. This second sentence "
        "continues with additional details about the topic being described on this page. "
        "And this third sentence goes even further into the subject matter to ensure "
        "we have enough content to properly evaluate the sentence boundary detection."
    )
    desc = _extract_description(text)
    assert desc.endswith(".") or desc.endswith("!") or desc.endswith("?")
    assert len(desc) >= 100  # Should be substantive
    # Should not end mid-sentence (i.e., the last char before the period should not be a space)
    assert not desc.endswith(" .")


def test_skips_boilerplate():
    """Boilerplate lines (cookie, nav, short lines) should be skipped."""
    text = (
        "This website uses cookies to improve your experience.\n"
        "Navigation\n"
        "Sign in to your account\n"
        "Skip to main content\n"
        "The real page content begins here and provides the actual value "
        "for readers who want to learn about the topic being discussed."
    )
    desc = _extract_description(text)
    assert "cookie" not in desc.lower()
    assert "navigation" not in desc.lower()
    assert "real page content" in desc


def test_skips_short_lines():
    """Very short lines under 30 chars should be filtered out."""
    text = (
        "Home\n"
        "About\n"
        "Pricing\n"
        "Contact\n"
        "The main article content starts here and provides substantial "
        "information about the topic that readers are interested in reading."
    )
    desc = _extract_description(text)
    assert "Home" not in desc
    assert len(desc) >= 50


def test_skips_headings():
    """Headings, images, and blockquotes should not be included in descriptions."""
    text = (
        "# Page Title\n"
        "![image](img.jpg)\n"
        "> A blockquote that should be skipped\n"
        "- A list item that should be skipped when it starts the line\n"
        "The actual paragraph content begins here and provides thorough "
        "information about the page's subject matter for the reader."
    )
    desc = _extract_description(text)
    assert "Page Title" not in desc
    assert "actual paragraph content" in desc


def test_joins_short_candidates():
    """When the first candidate is short, subsequent candidates should be appended."""
    text = (
        "Short line.\n"
        "Second short line with more words.\n"
        "A longer sentence that brings the total description up to a reasonable "
        "length for testing the candidate joining behavior in the extraction function."
    )
    desc = _extract_description(text)
    assert len(desc) >= 50


def test_returns_empty_for_empty_input():
    """Empty input should return empty string."""
    assert _extract_description("") == ""
    assert _extract_description("   ") == ""


def test_fallback_truncation():
    """When no sentence boundary is found, text should be truncated with ellipsis."""
    # Create text with no sentence-ending punctuation
    text = "This is a very long string of text that has no sentence boundaries " * 20
    desc = _extract_description(text)
    if len(desc) >= 300:
        assert desc.endswith("..."), (
            f"Should end with ellipsis when truncated, got: {desc[-20:]}"
        )


def test_multi_sentence_content():
    """Multi-sentence content should include complete first sentence at minimum."""
    text = (
        "This is the opening statement of the page. Here is an additional "
        "sentence that provides supplementary details. And this is a third "
        "sentence that rounds out the introductory paragraph content nicely."
    )
    desc = _extract_description(text)
    # Should include at least the first full sentence
    assert "opening statement" in desc
    # Should end with a sentence-ending punctuation
    assert desc.rstrip()[-1] in ".!?"


# ── discover_pages tests ──────────────────────────────────────────


def _mock_html_page(
    *,
    internal_links: list[str] | None = None,
    external_links: list[str] | None = None,
    non_http_links: list[str] | None = None,
) -> str:
    """Build an HTML page with the given link lists for testing."""
    links_html = ""
    for link in internal_links or []:
        links_html += f'<a href="{link}">internal</a>\n'
    for link in external_links or []:
        links_html += f'<a href="{link}">external</a>\n'
    for link in non_http_links or []:
        links_html += f'<a href="{link}">non-http</a>\n'
    return f"<html><body>{links_html}</body></html>"


@pytest.mark.asyncio
async def test_discover_pages_extracts_internal_links():
    """discover_pages should extract same-host links from the page HTML."""
    html = _mock_html_page(
        internal_links=["/page1", "/page2", "/page3"],
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = html

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await discover_pages("http://example.com", max_pages=10)

    assert "http://example.com/page1" in result
    assert "http://example.com/page2" in result
    assert "http://example.com/page3" in result
    assert len(result) == 3


@pytest.mark.asyncio
async def test_discover_pages_excludes_external_links():
    """discover_pages should exclude links to external domains."""
    html = _mock_html_page(
        internal_links=["/page1"],
        external_links=["https://other.com/page"],
        non_http_links=["mailto:test@example.com"],
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = html

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await discover_pages("http://example.com", max_pages=10)

    assert "http://example.com/page1" in result
    assert "https://other.com/page" not in result
    assert "mailto:test@example.com" not in result
    assert len(result) == 1


@pytest.mark.asyncio
async def test_discover_pages_respects_max_pages():
    """discover_pages should limit results to max_pages."""
    html = _mock_html_page(
        internal_links=[f"/page{i}" for i in range(20)],
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = html

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await discover_pages("http://example.com", max_pages=3)

    assert len(result) == 3
    # First 3 links should be returned
    assert result[0] == "http://example.com/page0"
    assert result[1] == "http://example.com/page1"
    assert result[2] == "http://example.com/page2"


@pytest.mark.asyncio
async def test_discover_pages_http_error_falls_back():
    """discover_pages should return [url] when the server returns non-200."""
    mock_resp = AsyncMock()
    mock_resp.status_code = 404

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await discover_pages("http://example.com", max_pages=10)

    assert result == ["http://example.com"]


@pytest.mark.asyncio
async def test_discover_pages_exception_falls_back():
    """discover_pages should return [url] when an exception occurs during fetch."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.side_effect = Exception("Connection error")
        mock_client_cls.return_value = mock_client

        result = await discover_pages("http://example.com", max_pages=10)

    assert result == ["http://example.com"]


@pytest.mark.asyncio
async def test_discover_pages_empty_results_falls_back():
    """discover_pages should return [url] when no links are found."""
    html = "<html><body><p>No links here</p></body></html>"
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = html

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await discover_pages("http://example.com", max_pages=10)

    assert result == ["http://example.com"]


@pytest.mark.asyncio
async def test_discover_pages_deduplicates_links():
    """discover_pages should not return duplicate URLs."""
    html = _mock_html_page(
        internal_links=["/page1", "/page1", "/page2", "/page1"],
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = html

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await discover_pages("http://example.com", max_pages=10)

    assert len(result) == 2
    assert "http://example.com/page1" in result
    assert "http://example.com/page2" in result


@pytest.mark.asyncio
async def test_discover_pages_strips_fragments():
    """discover_pages should strip fragment identifiers from URLs."""
    html = _mock_html_page(
        internal_links=["/page1#section1", "/page1#section2", "/page2"],
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = html

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await discover_pages("http://example.com", max_pages=10)

    # Fragment-stripped URLs should be deduped — only one /page1 entry
    assert "http://example.com/page1" in result
    assert "http://example.com/page2" in result
    assert "#" not in str(result)
    assert len(result) == 2


@pytest.mark.asyncio
async def test_discover_pages_max_pages_larger_than_available():
    """When max_pages exceeds available links, all links are returned."""
    html = _mock_html_page(
        internal_links=["/a", "/b"],
    )
    mock_resp = AsyncMock()
    mock_resp.status_code = 200
    mock_resp.text = html

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = await discover_pages("http://example.com", max_pages=100)

    assert len(result) == 2
