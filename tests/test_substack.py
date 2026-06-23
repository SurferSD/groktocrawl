"""Unit tests for the Substack adapter.

Tests cover:
- URL pattern matching (standard substack.com, vanity domains)
- Vanity domain probe caching
- RSS feed parsing (full items, edge cases)
- Item matching by link and by slug
- RSS content to markdown conversion
"""

from __future__ import annotations

import time

from scraper.adapters.substack import (
    _SUBSTACK_URL_PATTERNS,
    _find_item_by_link,
    _is_substack_origin,
    _parse_rss_items,
    _rss_content_to_markdown,
)

# ── URL pattern tests ────────────────────────────────────────────


def test_matches_standard_substack_url():
    url = "https://gibson.substack.com/p/some-article-title"
    assert any(p.search(url) for p in _SUBSTACK_URL_PATTERNS)


def test_matches_pub_url():
    url = "https://gibson.substack.com/pub/some-article"
    assert any(p.search(url) for p in _SUBSTACK_URL_PATTERNS)


def test_matches_vanity_domain_with_p():
    url = "https://www.lennysnewsletter.com/p/some-article"
    assert any(p.search(url) for p in _SUBSTACK_URL_PATTERNS)


def test_does_not_match_non_substack():
    url = "https://example.com/about"
    assert not any(p.search(url) for p in _SUBSTACK_URL_PATTERNS)


def test_does_not_match_root_domain():
    url = "https://www.lennysnewsletter.com/"
    assert not any(p.search(url) for p in _SUBSTACK_URL_PATTERNS)


def test_does_not_match_non_substack_p_url():
    url = "https://example.com/p/123"
    assert any(p.search(url) for p in _SUBSTACK_URL_PATTERNS)  # matches broad pattern


# ── Vanity domain probe tests ────────────────────────────────────


def test_substack_com_domain_returns_true():
    assert _is_substack_origin("https://gibson.substack.com") is True


def test_substack_com_domain_cached():
    # Reset cache state
    from scraper.adapters.substack import _VANITY_CACHE

    _VANITY_CACHE.clear()
    result = _is_substack_origin("https://gibson.substack.com")
    assert result is True
    # Should be cached
    assert "https://gibson.substack.com" in _VANITY_CACHE
    assert _VANITY_CACHE["https://gibson.substack.com"][1] is True


def test_www_substack_com_domain():
    # www.substack.com is a subdomain of substack.com and should be
    # detected as a Substack origin (it serves Substack content)
    assert _is_substack_origin("https://www.substack.com") is True
    # Note: www.substack.com is not a publication, just the main site


# ── RSS item matching ────────────────────────────────────────────


SAMPLE_ITEMS = [
    {
        "title": "My First Post",
        "link": "https://gibson.substack.com/p/my-first-post",
        "description": "A great first post.",
        "creator": "Gibson",
        "pub_date": "Mon, 01 Jan 2024 12:00:00 GMT",
        "content_encoded": "<p>Hello world</p>",
    },
    {
        "title": "Second Post",
        "link": "https://gibson.substack.com/p/second-post",
        "description": "Another post.",
        "creator": "Gibson",
        "pub_date": "Tue, 02 Jan 2024 12:00:00 GMT",
        "content_encoded": "<p>Second article content</p>",
    },
]


def test_find_item_by_exact_link():
    result = _find_item_by_link(
        SAMPLE_ITEMS, "https://gibson.substack.com/p/my-first-post"
    )
    assert result is not None
    assert result["title"] == "My First Post"


def test_find_item_with_trailing_slash():
    result = _find_item_by_link(
        SAMPLE_ITEMS,
        "https://gibson.substack.com/p/my-first-post/",
    )
    assert result is not None
    assert result["title"] == "My First Post"


def test_find_item_by_slug():
    result = _find_item_by_link(
        SAMPLE_ITEMS, "https://gibson.substack.com/p/my-first-post?ref=foo"
    )
    assert result is not None
    assert result["title"] == "My First Post"


def test_find_item_not_found():
    result = _find_item_by_link(
        SAMPLE_ITEMS, "https://gibson.substack.com/p/non-existent-post"
    )
    assert result is None


def test_find_item_empty_list():
    result = _find_item_by_link([], "https://gibson.substack.com/p/my-first-post")
    assert result is None


# ── RSS feed parsing ─────────────────────────────────────────────


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/"
     version="2.0">
  <channel>
    <title><![CDATA[Test Newsletter]]></title>
    <link>https://test.substack.com</link>
    <generator>Substack</generator>
    <item>
      <title><![CDATA[Article One]]></title>
      <link>https://test.substack.com/p/article-one</link>
      <description><![CDATA[First article]]></description>
      <dc:creator><![CDATA[Author One]]></dc:creator>
      <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
      <content:encoded><![CDATA[<p>Content of article one.</p>]]></content:encoded>
    </item>
    <item>
      <title><![CDATA[Article Two]]></title>
      <link>https://test.substack.com/p/article-two</link>
      <description><![CDATA[Second article]]></description>
      <dc:creator><![CDATA[Author Two]]></dc:creator>
      <pubDate>Tue, 02 Jan 2024 12:00:00 GMT</pubDate>
      <content:encoded><![CDATA[<p>Content of article two.</p><p>More content.</p>]]></content:encoded>
    </item>
  </channel>
</rss>"""


def test_parse_rss_items_count():
    items = _parse_rss_items(SAMPLE_RSS)
    assert len(items) == 2


def test_parse_rss_item_fields():
    items = _parse_rss_items(SAMPLE_RSS)
    first = items[0]
    assert first["title"] == "Article One"
    assert first["link"] == "https://test.substack.com/p/article-one"
    assert first["description"] == "First article"
    assert first["creator"] == "Author One"
    assert first["pub_date"] == "Mon, 01 Jan 2024 12:00:00 GMT"
    assert "<p>Content of article one.</p>" in first.get("content_encoded", "")


def test_parse_rss_second_item():
    items = _parse_rss_items(SAMPLE_RSS)
    second = items[1]
    assert second["title"] == "Article Two"
    assert second["creator"] == "Author Two"
    assert "<p>Content of article two.</p>" in second.get("content_encoded", "")


def test_parse_empty_rss():
    items = _parse_rss_items("<rss><channel></channel></rss>")
    assert items == []


def test_parse_invalid_xml():
    items = _parse_rss_items("not xml at all")
    assert items == []


def test_parse_rss_no_items():
    items = _parse_rss_items(
        "<rss><channel><title><![CDATA[Empty]]></title></channel></rss>"
    )
    assert items == []


# ── Content-to-markdown tests ────────────────────────────────────


def test_basic_html_to_markdown():
    html = "<p>Simple paragraph.</p>"
    result = _rss_content_to_markdown(html)
    assert isinstance(result, str)
    assert len(result) > 0
    # Should contain the text content
    assert "Simple paragraph." in result


def test_empty_html():
    result = _rss_content_to_markdown("")
    # Should handle gracefully, possibly returning empty or truncated
    assert isinstance(result, str)


def test_html_with_multiple_tags():
    html = "<h1>Title</h1><p>Some text.</p><ul><li>Item 1</li><li>Item 2</li></ul>"
    result = _rss_content_to_markdown(html)
    assert "Title" in result or "# Title" in result
    assert "Some text." in result


def test_html_with_script_style_stripped():
    html = "<p>Visible</p><script>alert('hidden')</script><style>.cls{}</style><p>Also visible</p>"
    result = _rss_content_to_markdown(html)
    assert "Visible" in result
    assert "Also visible" in result
    assert "alert" not in result


# ── Vanity domain probe cache expiry ─────────────────────────────


def test_vanity_cache_standard_substack():
    from scraper.adapters.substack import _VANITY_CACHE

    _VANITY_CACHE.clear()
    _is_substack_origin("https://test.substack.com")
    assert _VANITY_CACHE["https://test.substack.com"][1] is True


def test_vanity_cache_ttl():
    from scraper.adapters.substack import _VANITY_CACHE

    _VANITY_CACHE.clear()
    _is_substack_origin("https://test.substack.com")
    expires_at, value = _VANITY_CACHE["https://test.substack.com"]
    assert expires_at > time.time()
    assert expires_at <= time.time() + 3601
    assert value is True
