"""Tests for the structured metadata extraction module.

Unit tests — no Docker needed. Run directly:
    python -m pytest tests/test_metadata.py -v
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper-svc"))

from scraper.metadata import (
    extract_all_metadata,
    extract_json_ld,
    extract_og_tags,
    extract_standard_meta,
    extract_twitter_tags,
)

# ── Test HTML fixtures ───────────────────────────────────────────

SIMPLE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <title>Test Page</title>
    <meta name="description" content="A test page for metadata extraction">
    <meta name="author" content="Test Author">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="index, follow">
    <meta name="generator" content="TestCMS 1.0">
    <meta property="og:title" content="OG Test Title">
    <meta property="og:description" content="OG test description">
    <meta property="og:image" content="https://example.com/image.jpg">
    <meta property="og:type" content="article">
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="Twitter Test Title">
    <meta name="twitter:description" content="Twitter test description">
    <link rel="canonical" href="https://example.com/test-page">
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","headline":"Test Article","author":{"@type":"Person","name":"Jane Doe"}}</script>
</head>
<body><h1>Test</h1></body>
</html>"""


MULTI_JSON_LD_HTML = """<!DOCTYPE html>
<html>
<head>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"Article","headline":"Article One"}</script>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Widget","offers":{"@type":"AggregateOffer","priceCurrency":"USD","lowPrice":"9.99"}}</script>
</head>
<body></body>
</html>"""


GRAPH_JSON_LD_HTML = """<!DOCTYPE html>
<html>
<head>
    <script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Article","headline":"Article One"},{"@type":"Person","name":"Author Name"}]}</script>
</head>
<body></body>
</html>"""


LIST_JSON_LD_HTML = """<!DOCTYPE html>
<html>
<head>
    <script type="application/ld+json">[{"@type":"Article","headline":"First"},{"@type":"Article","headline":"Second"}]</script>
</head>
<body></body>
</html>"""


MULTI_OG_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta property="og:image" content="https://example.com/img1.jpg">
    <meta property="og:image" content="https://example.com/img2.jpg">
    <meta property="og:image:width" content="1200">
    <meta property="og:title" content="Single Title">
</head>
<body></body>
</html>"""


LANGUAGE_META_HTML = """<!DOCTYPE html>
<html lang="fr">
<head>
    <meta name="author" content="Auteur">
    <meta http-equiv="content-language" content="fr">
</head>
<body></body>
</html>"""


PUB_DATE_HTML = """<!DOCTYPE html>
<html>
<head>
    <meta property="article:published_time" content="2026-06-05T12:00:00Z">
    <meta property="article:modified_time" content="2026-06-06T14:30:00Z">
    <meta name="keywords" content="metadata, scraping, json-ld">
</head>
<body></body>
</html>"""


NO_META_HTML = """<!DOCTYPE html>
<html><head><title>Minimal</title></head><body><p>No metadata here.</p></body></html>"""


# ── extract_json_ld ──────────────────────────────────────────────


def test_json_ld_empty_html():
    assert extract_json_ld("") == []


def test_json_ld_single_block():
    result = extract_json_ld(SIMPLE_HTML)
    assert len(result) == 1
    assert result[0]["@type"] == "Article"
    assert result[0]["headline"] == "Test Article"
    assert result[0]["author"]["name"] == "Jane Doe"


def test_json_ld_multiple_blocks():
    result = extract_json_ld(MULTI_JSON_LD_HTML)
    assert len(result) == 2
    types = {r["@type"] for r in result}
    assert types == {"Article", "Product"}
    names = {r.get("headline") or r.get("name") for r in result}
    assert names == {"Article One", "Widget"}


def test_json_ld_graph_unwrap():
    """@graph arrays get unwrapped into individual items."""
    result = extract_json_ld(GRAPH_JSON_LD_HTML)
    assert len(result) == 2
    types = {r["@type"] for r in result}
    assert types == {"Article", "Person"}


def test_json_ld_list():
    """Top-level JSON arrays are unwrapped."""
    result = extract_json_ld(LIST_JSON_LD_HTML)
    assert len(result) == 2
    assert result[0]["headline"] == "First"
    assert result[1]["headline"] == "Second"


def test_json_ld_no_blocks():
    result = extract_json_ld(NO_META_HTML)
    assert result == []


def test_json_ld_invalid_json():
    """Invalid JSON blocks are silently skipped."""
    html = '<script type="application/ld+json">{invalid json}</script>'
    result = extract_json_ld(html)
    assert result == []


def test_json_ld_empty_block():
    """Empty script blocks are silently skipped."""
    html = '<script type="application/ld+json"></script>'
    result = extract_json_ld(html)
    assert result == []


# ── extract_og_tags ──────────────────────────────────────────────


def test_og_empty_html():
    assert extract_og_tags("") == {}


def test_og_basic():
    result = extract_og_tags(SIMPLE_HTML)
    assert result["title"] == "OG Test Title"
    assert result["description"] == "OG test description"
    assert result["type"] == "article"
    assert result["image"] == "https://example.com/image.jpg"


def test_og_multi_image():
    """Multi-valued og:image properties are collected into lists."""
    result = extract_og_tags(MULTI_OG_HTML)
    assert result["title"] == "Single Title"  # Single value stays flat
    assert isinstance(result["image"], list)
    assert len(result["image"]) == 2
    assert "img1.jpg" in result["image"][0]
    assert "img2.jpg" in result["image"][1]
    # Non-image multi-valued property also collects
    assert result["image:width"] == "1200"


def test_og_no_tags():
    result = extract_og_tags(NO_META_HTML)
    assert result == {}


# ── extract_twitter_tags ──────────────────────────────────────────


def test_twitter_empty_html():
    assert extract_twitter_tags("") == {}


def test_twitter_basic():
    result = extract_twitter_tags(SIMPLE_HTML)
    assert result["card"] == "summary_large_image"
    assert result["title"] == "Twitter Test Title"
    assert result["description"] == "Twitter test description"


def test_twitter_no_tags():
    result = extract_twitter_tags(NO_META_HTML)
    assert result == {}


# ── extract_standard_meta ────────────────────────────────────────


def test_meta_empty_html():
    assert extract_standard_meta("") == {}


def test_meta_basic():
    result = extract_standard_meta(SIMPLE_HTML)
    assert result["description"] == "A test page for metadata extraction"
    assert result["author"] == "Test Author"
    assert result["canonical"] == "https://example.com/test-page"
    assert result["language"] == "en"
    assert result["viewport"] == "width=device-width, initial-scale=1"
    assert result["robots"] == "index, follow"
    assert result["generator"] == "TestCMS 1.0"


def test_meta_language_from_html():
    """Language should come from <html lang> before content-language."""
    result = extract_standard_meta(LANGUAGE_META_HTML)
    assert result["language"] == "fr"
    assert result["author"] == "Auteur"


def test_meta_publication_date():
    result = extract_standard_meta(PUB_DATE_HTML)
    assert result["publication_date"] == "2026-06-05T12:00:00Z"
    assert result["modified_date"] == "2026-06-06T14:30:00Z"
    assert result["keywords"] == "metadata, scraping, json-ld"


def test_meta_no_tags():
    result = extract_standard_meta(NO_META_HTML)
    # No lang attribute on <html> in NO_META_HTML
    assert "language" not in result
    assert "description" not in result


# ── extract_all_metadata (integration) ───────────────────────────


def test_all_metadata_empty():
    result = extract_all_metadata("")
    assert result == {"json_ld": [], "og": {}, "twitter": {}, "meta": {}}


def test_all_metadata_whitespace():
    result = extract_all_metadata("   ")
    assert result == {"json_ld": [], "og": {}, "twitter": {}, "meta": {}}


def test_all_metadata_comprehensive():
    """Full extraction from a page with all metadata types."""
    result = extract_all_metadata(SIMPLE_HTML)

    # JSON-LD
    assert len(result["json_ld"]) == 1
    assert result["json_ld"][0]["@type"] == "Article"

    # OG
    assert result["og"]["title"] == "OG Test Title"
    assert result["og"]["type"] == "article"

    # Twitter
    assert result["twitter"]["card"] == "summary_large_image"

    # Meta
    assert result["meta"]["description"] == "A test page for metadata extraction"
    assert result["meta"]["language"] == "en"


def test_all_metadata_no_data():
    """A page with no structured metadata returns empty containers."""
    result = extract_all_metadata(NO_META_HTML)
    assert result["json_ld"] == []
    assert result["og"] == {}
    assert result["twitter"] == {}
    assert result["meta"] == {}


def test_all_metadata_product_offer():
    """E-commerce JSON-LD with product/offer data is preserved."""
    result = extract_all_metadata(MULTI_JSON_LD_HTML)
    products = [r for r in result["json_ld"] if r["@type"] == "Product"]
    assert len(products) == 1
    assert products[0]["offers"]["lowPrice"] == "9.99"
    assert products[0]["offers"]["priceCurrency"] == "USD"
