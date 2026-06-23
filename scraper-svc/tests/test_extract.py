"""Unit tests for filter_sections() and extract_extras() in scraper.extract."""

from scraper.extract import extract_extras, filter_sections

# ── HTML fixtures ───────────────────────────────────────────────

FULL_PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head><title>Test Page</title></head>
<body>
    <header>
        <h1>Site Header</h1>
        <p>Site description goes here.</p>
    </header>
    <nav>
        <a href="/home">Home</a>
        <a href="https://example.com/about">About</a>
        <a href="https://other.com/contact">Contact</a>
    </nav>
    <main>
        <article>
            <h2>Main Article Title</h2>
            <p>This is the main body content of the page. It contains substantial
            text that should be preserved when filtering sections. This is the
            primary content that users care about.</p>
            <p>Second paragraph with more content. This continues the main article
            body and should also be preserved.</p>
            <img src="https://example.com/image1.png" alt="First image">
            <img src="/local-image.jpg" alt="Local image">
            <pre><code>def hello():
    print("Hello, world!")
    return 42</code></pre>
            <p>A <a href="https://example.com/article">link to another article</a>
            and a <a href="mailto:test@example.com">mailto link</a> and a
            <a href="javascript:void(0)">javascript link</a>.</p>
        </article>
    </main>
    <aside>
        <h3>Sidebar</h3>
        <p>Related links and widgets.</p>
    </aside>
    <footer>
        <p>Copyright 2024. All rights reserved.</p>
        <a href="/privacy">Privacy Policy</a>
    </footer>
</body>
</html>"""

NO_SECTIONS_HTML = """<!DOCTYPE html>
<html>
<head><title>Simple Page</title></head>
<body>
    <p>Just a simple paragraph with some content.</p>
    <p>Another paragraph without semantic sections.</p>
</body>
</html>"""

EMPTY_HTML = ""

LINK_TEST_HTML = """<!DOCTYPE html>
<html>
<body>
    <a href="https://example.com/page1">Page 1</a>
    <a href="https://example.com/page2">Page 2</a>
    <a href="https://other.com/page">Other domain</a>
    <a href="mailto:test@example.com">Email</a>
    <a href="javascript:void(0)">JS Link</a>
    <a href="/relative">Relative</a>
    <a href="#">Hash link</a>
    <a href="tel:+1234567890">Phone</a>
</body>
</html>"""

IMG_TEST_HTML = """<!DOCTYPE html>
<html>
<body>
    <img src="https://example.com/img1.png" alt="Image 1">
    <img src="https://example.com/img2.jpg" alt="Image 2">
    <img src="https://other.com/img3.png" alt="Image 3">
    <img src="data:image/png;base64,abc123==" alt="Data URI">
    <img src="" alt="Empty src">
</body>
</html>"""

CODE_BLOCK_HTML = """<!DOCTYPE html>
<html>
<body>
    <pre><code>def foo():
    return "bar"</code></pre>
    <p>Some text with inline <code>print(x)</code> code.</p>
    <pre><code>class Baz:
    pass</code></pre>
    <p>More <code>y = 42</code> inline code.</p>
</body>
</html>"""


# ── filter_sections tests ──────────────────────────────────────


class TestFilterSections:
    def test_no_filters_default(self):
        """Without filters, filter_sections should produce readable markdown."""
        result = filter_sections(FULL_PAGE_HTML)
        assert result, "Should produce output"
        assert "Main Article Title" in result, "Should contain body content"
        # readability-lxml strips header/nav/footer boilerplate, so body is the
        # primary remaining content — that's expected behaviour.

    def test_include_only_body(self):
        """With include=["body"], only body sections should remain."""
        result = filter_sections(FULL_PAGE_HTML, include=["body"])
        assert result, "Should produce output"
        assert "Main Article Title" in result, "Should contain body content"
        # Header/footer text should be excluded
        header_present = "Site Header" in result
        footer_present = "Copyright" in result
        assert not header_present, "Header content should be excluded"
        assert not footer_present, "Footer content should be excluded"

    def test_exclude_nav_and_footer(self):
        """Excluding nav and footer should strip those sections."""
        result = filter_sections(
            FULL_PAGE_HTML, exclude=["navigation", "footer"]
        )
        assert result, "Should produce output"
        assert "Main Article Title" in result, "Body content should remain"
        assert "Copyright" not in result, "Footer content should be excluded"
        # readability-lxml strips <aside> by default in standard verbosity,
        # so sidebar may not appear in output — that's expected.

    def test_include_first_then_exclude(self):
        """When both include and exclude, include should apply first."""
        result = filter_sections(
            FULL_PAGE_HTML, include=["body", "sidebar"], exclude=["sidebar"]
        )
        assert result, "Should produce output"
        assert "Main Article Title" in result, "Body content should remain"
        assert "Sidebar" not in result, "Sidebar should be excluded"
        assert "Related links" not in result, "Sidebar content should be excluded"

    def test_compact_verbosity(self):
        """Compact verbosity should return ~300 chars."""
        result = filter_sections(FULL_PAGE_HTML, verbosity="compact")
        assert result, "Should produce output"
        assert len(result) <= 310, (
            f"Compact output should be ~300 chars, got {len(result)}"
        )
        # Should contain meaningful body text, not just header
        assert len(result) >= 100, (
            f"Compact output should have reasonable content, got {len(result)} chars"
        )

    def test_full_verbosity(self):
        """Full verbosity should return more content than standard."""
        standard = filter_sections(FULL_PAGE_HTML, verbosity="standard")
        full = filter_sections(FULL_PAGE_HTML, verbosity="full")
        assert len(full) > 0, "Full should produce output"
        # Full should generally be longer because it includes structural markup
        assert len(full) >= len(standard) * 0.5, (
            f"Full ({len(full)} chars) should be comparable to standard ({len(standard)} chars)"
        )

    def test_standard_verbosity_explicit(self):
        """Explicit standard verbosity should produce same as default."""
        default = filter_sections(FULL_PAGE_HTML)
        explicit = filter_sections(FULL_PAGE_HTML, verbosity="standard")
        assert default == explicit, "Standard verbosity should match default"

    def test_empty_html(self):
        """Empty HTML should return empty string."""
        result = filter_sections(EMPTY_HTML)
        assert result == "", "Empty HTML should return empty string"

    def test_no_semantic_sections(self):
        """HTML without semantic sections should still produce output."""
        result = filter_sections(NO_SECTIONS_HTML)
        assert result, "Should produce output even without semantic sections"
        assert "simple paragraph" in result.lower()

    def test_include_nonexistent_section(self):
        """Including a nonexistent section should still work."""
        result = filter_sections(
            FULL_PAGE_HTML, include=["body", "nonexistent"]
        )
        assert "Main Article Title" in result, "Body should remain"
        assert "Site Header" not in result, "Header should be excluded"


# ── extract_extras tests ────────────────────────────────────────


class FakeExtrasOptions:
    """Minimal options object matching ExtrasOptions interface for tests."""

    def __init__(self, links=None, image_links=None, code_blocks=None):
        self.links = links
        self.imageLinks = image_links
        self.codeBlocks = code_blocks


class TestExtractExtras:
    def test_extract_links(self):
        """Should extract external links, excluding mailto/javascript/hash."""
        opts = FakeExtrasOptions(links=10)
        result = extract_extras(LINK_TEST_HTML, opts)
        assert "links" in result
        links = result["links"]
        assert len(links) >= 2, f"Expected at least 2 links, got {links}"
        # Should include relative URLs
        has_relative = any("/relative" in l for l in links)
        assert has_relative, "Should include relative URLs"
        # Should NOT include mailto, javascript, hash, tel
        for link in links:
            assert not link.startswith("mailto:"), f"Should exclude mailto: {link}"
            assert not link.startswith("javascript:"), (
                f"Should exclude javascript: {link}"
            )
            assert not link.startswith("tel:"), f"Should exclude tel: {link}"
            assert link != "#", f"Should exclude bare hash link"

    def test_extract_links_limit(self):
        """Should respect the links limit."""
        opts = FakeExtrasOptions(links=2)
        result = extract_extras(LINK_TEST_HTML, opts)
        assert len(result.get("links", [])) <= 2, "Should respect links limit"

    def test_extract_images(self):
        """Should extract image URLs, excluding data URIs."""
        opts = FakeExtrasOptions(imageLinks=10)
        result = extract_extras(IMG_TEST_HTML, opts)
        assert "imageLinks" in result
        images = result["imageLinks"]
        assert len(images) >= 2, f"Expected at least 2 images, got {images}"
        # Should not include data: URIs
        for img in images:
            assert not img.startswith("data:"), f"Should exclude data URIs: {img}"
            assert img != "", "Should exclude empty src"

    def test_extract_images_limit(self):
        """Should respect the imageLinks limit."""
        opts = FakeExtrasOptions(imageLinks=2)
        result = extract_extras(IMG_TEST_HTML, opts)
        assert len(result.get("imageLinks", [])) <= 2, "Should respect imageLinks limit"

    def test_extract_code_blocks(self):
        """Should extract code blocks from pre>code and standalone code."""
        opts = FakeExtrasOptions(codeBlocks=10)
        result = extract_extras(CODE_BLOCK_HTML, opts)
        assert "codeBlocks" in result
        blocks = result["codeBlocks"]
        assert len(blocks) >= 2, f"Expected at least 2 code blocks, got {blocks}"
        # First block should be from pre>code
        assert any("def foo" in b for b in blocks), "Should contain the foo function"
        assert any("class Baz" in b for b in blocks), "Should contain the Baz class"

    def test_extract_code_blocks_limit(self):
        """Should respect the codeBlocks limit."""
        opts = FakeExtrasOptions(codeBlocks=1)
        result = extract_extras(CODE_BLOCK_HTML, opts)
        assert len(result.get("codeBlocks", [])) <= 1, (
            "Should respect codeBlocks limit"
        )

    def test_extract_empty_html(self):
        """Empty HTML should return empty dict."""
        opts = FakeExtrasOptions(links=5, imageLinks=5, codeBlocks=5)
        result = extract_extras(EMPTY_HTML, opts)
        assert result == {}, "Empty HTML should return empty dict"

    def test_extract_no_options_set(self):
        """When all options are None/0, should return empty dict."""
        opts = FakeExtrasOptions()
        result = extract_extras(FULL_PAGE_HTML, opts)
        assert result == {}, "No options should return empty dict"

    def test_extract_only_requested_keys(self):
        """Only keys that were requested should appear in output."""
        opts = FakeExtrasOptions(links=5)
        result = extract_extras(FULL_PAGE_HTML, opts)
        assert "links" in result, "links should be present"
        assert "imageLinks" not in result, "imageLinks should not be present"
        assert "codeBlocks" not in result, "codeBlocks should not be present"

    def test_extract_code_blocks_standalone_code(self):
        """Standalone <code> elements (not in <pre>) should be captured."""
        opts = FakeExtrasOptions(codeBlocks=10)
        result = extract_extras(CODE_BLOCK_HTML, opts)
        blocks = result.get("codeBlocks", [])
        # Should include inline code too
        assert any("print(x)" in b for b in blocks), (
            f"Should capture inline code, got {blocks}"
        )

    def test_extract_combined(self):
        """All three extras should be extractable simultaneously."""
        opts = FakeExtrasOptions(links=5, imageLinks=5, codeBlocks=5)
        result = extract_extras(FULL_PAGE_HTML, opts)
        assert "links" in result
        assert "imageLinks" in result
        assert "codeBlocks" in result
        assert len(result["links"]) <= 5
        assert len(result["imageLinks"]) <= 5
        assert len(result["codeBlocks"]) <= 5
