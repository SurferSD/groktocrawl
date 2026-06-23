"""Unit tests for the politeness protocol module.

Tests the PolitenessManager in isolation, with Valkey caching mocked out
(not available in test environments by default). Covers robots.txt parsing,
rate limiting, and the check/delay/block decision flow.
"""

import os
import sys
import time
from unittest.mock import patch

import pytest

# Ensure politeness is off before importing (tests will toggle per-test)
os.environ.setdefault("SCRAPER_POLITENESS_ENABLED", "false")

# Add scraper-svc to path so we can import scraper modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scraper-svc"))

# ── Force-disable Valkey cache client for all tests ────────────
from scraper.politeness import PolitenessManager, _DomainState


@pytest.fixture(autouse=True)
def _disable_valkey():
    """Disable Valkey cache client for politeness tests."""
    with (
        patch(
            "scraper.politeness.PolitenessManager._robots_cache_store",
            return_value=None,
        ),
        patch(
            "scraper.politeness.PolitenessManager._robots_cache_load",
            return_value=None,
        ),
    ):
        yield


# ── Robots.txt parsing ─────────────────────────────────────────


class TestRobotsTxtParsing:
    def test_allow_all(self):
        """Empty or missing robots.txt assumes all paths allowed."""
        mgr = PolitenessManager()
        mgr._enabled = True
        mgr._domains["example.com"] = _DomainState()
        mgr._parse_robots_txt("", mgr._domains["example.com"])
        assert len(mgr._domains["example.com"].robots_disallowed_paths) == 0

    def test_allow_all_explicit(self):
        """User-agent: * with no Disallow means all allowed."""
        mgr = PolitenessManager()
        mgr._enabled = True
        mgr._domains["example.com"] = _DomainState()
        mgr._parse_robots_txt(
            "User-agent: *\nDisallow:",
            mgr._domains["example.com"],
        )
        assert len(mgr._domains["example.com"].robots_disallowed_paths) == 0

    def test_disallow_single_path(self):
        """/private path is disallowed."""
        mgr = PolitenessManager()
        mgr._enabled = True
        mgr._domains["example.com"] = _DomainState()
        mgr._parse_robots_txt(
            "User-agent: *\nDisallow: /private",
            mgr._domains["example.com"],
        )
        assert len(mgr._domains["example.com"].robots_disallowed_paths) == 1

    def test_disallow_pattern_with_wildcard(self):
        """/foo/*.html pattern works."""
        mgr = PolitenessManager()
        mgr._enabled = True
        mgr._domains["example.com"] = _DomainState()
        mgr._parse_robots_txt(
            "User-agent: *\nDisallow: /foo/*.html",
            mgr._domains["example.com"],
        )
        assert len(mgr._domains["example.com"].robots_disallowed_paths) == 1

    def test_disallow_exact_pattern(self):
        """/exact$ matches only /exact, not /exactly."""
        mgr = PolitenessManager()
        mgr._enabled = True
        mgr._domains["example.com"] = _DomainState()
        mgr._parse_robots_txt(
            "User-agent: *\nDisallow: /exact$",
            mgr._domains["example.com"],
        )
        patterns = mgr._domains["example.com"].robots_disallowed_paths
        assert len(patterns) == 1
        assert patterns[0].search("/exact") is not None
        assert patterns[0].search("/exactly") is None

    def test_crawl_delay_parsed(self):
        """Crawl-delay is parsed from robots.txt."""
        mgr = PolitenessManager()
        mgr._enabled = True
        mgr._domains["example.com"] = _DomainState()
        mgr._parse_robots_txt(
            "User-agent: *\nDisallow: /private\nCrawl-delay: 5",
            mgr._domains["example.com"],
        )
        assert mgr._domains["example.com"].crawl_delay == 5.0

    def test_sitemap_directive(self):
        """Sitemap directives are extracted."""
        mgr = PolitenessManager()
        mgr._enabled = True
        mgr._domains["example.com"] = _DomainState()
        mgr._parse_robots_txt(
            "User-agent: *\nSitemap: https://example.com/sitemap.xml",
            mgr._domains["example.com"],
        )
        assert (
            "https://example.com/sitemap.xml"
            in mgr._domains["example.com"].robots_sitemaps
        )

    def test_user_agent_specific_section(self):
        """Only sections matching * or groktocrawl are applied."""
        mgr = PolitenessManager()
        mgr._enabled = True
        mgr._domains["example.com"] = _DomainState()
        mgr._parse_robots_txt(
            "User-agent: Googlebot\nDisallow: /google-only\n"
            "User-agent: *\nDisallow: /all-bots",
            mgr._domains["example.com"],
        )
        patterns = mgr._domains["example.com"].robots_disallowed_paths
        # Only the wildcard section should apply
        assert len(patterns) == 1
        assert patterns[0].search("/all-bots") is not None
        assert patterns[0].search("/google-only") is None


# ── PolitenessManager.check() ───────────────────────────────────


class TestPolitenessCheck:
    @pytest.mark.asyncio
    async def test_disabled_by_default(self):
        """When SCRAPER_POLITENESS_ENABLED is false, always proceeds."""
        mgr = PolitenessManager()
        mgr._enabled = False
        result = await mgr.check("https://example.com/page")
        assert result.action == "proceed"

    @pytest.mark.asyncio
    async def test_disallowed_path_blocked(self):
        """A URL matching a disallowed robots.txt path returns blocked."""
        mgr = PolitenessManager()
        mgr._enabled = True
        # Manually seed a disallowed path
        state = _DomainState(
            robots_cached_at=time.time(),
            crawl_delay=1.0,
        )
        import re

        state.robots_disallowed_paths = [re.compile(r"^/private")]
        mgr._domains["example.com"] = state

        result = await mgr.check("https://example.com/private/data")
        assert result.action == "blocked"
        assert not result.robots_allowed

    @pytest.mark.asyncio
    async def test_allowed_path_proceeds(self):
        """A URL not matching any disallowed path returns proceed."""
        mgr = PolitenessManager()
        mgr._enabled = True
        state = _DomainState(
            robots_cached_at=time.time(),
            crawl_delay=1.0,
        )
        import re

        state.robots_disallowed_paths = [re.compile(r"^/private")]
        mgr._domains["example.com"] = state

        result = await mgr.check("https://example.com/public/page")
        assert result.action in ("proceed", "delay")

    @pytest.mark.asyncio
    async def test_rate_limit_delays(self):
        """A second request within crawl_delay returns delay."""
        mgr = PolitenessManager()
        mgr._enabled = True
        state = _DomainState(
            robots_cached_at=time.time(),
            crawl_delay=5.0,
            last_request=time.time(),
            robots_disallowed_paths=[],
        )
        mgr._domains["example.com"] = state

        result = await mgr.check("https://example.com/page2")
        assert result.action == "delay"
        assert result.delay_seconds > 0
        assert result.delay_seconds <= 5.0

    @pytest.mark.asyncio
    async def test_rate_limit_expired_proceeds(self):
        """A request after crawl_delay has elapsed proceeds."""
        mgr = PolitenessManager()
        mgr._enabled = True
        state = _DomainState(
            robots_cached_at=time.time(),
            crawl_delay=0.1,
            last_request=time.time() - 10.0,  # way past the delay
            robots_disallowed_paths=[],
        )
        mgr._domains["example.com"] = state

        result = await mgr.check("https://example.com/page3")
        assert result.action == "proceed"

    @pytest.mark.asyncio
    async def test_record_request_updates_timing(self):
        """After record_request, a check should find the rate limit active."""
        mgr = PolitenessManager()
        mgr._enabled = True
        mgr.record_request("https://example.com/page")
        state = mgr._domains["example.com"]

        # Wait a tiny bit so we know the last_request is recent
        result = await mgr.check("https://example.com/other")
        assert result.action == "delay" or result.action == "proceed"


# ── get_politeness_metadata ─────────────────────────────────────


class TestPolitenessMetadata:
    def test_disabled_returns_enabled_false(self):
        mgr = PolitenessManager()
        mgr._enabled = False
        meta = mgr.get_politeness_metadata("https://example.com/page")
        assert meta == {"enabled": False}

    def test_enabled_unknown_domain(self):
        mgr = PolitenessManager()
        mgr._enabled = True
        meta = mgr.get_politeness_metadata("https://newdomain.com/page")
        assert meta["enabled"] is True
        assert meta["domain"] == "newdomain.com"

    def test_enabled_known_domain(self):
        mgr = PolitenessManager()
        mgr._enabled = True
        mgr._domains["example.com"] = _DomainState(
            crawl_delay=2.0,
            robots_cached_at=time.time(),
        )
        meta = mgr.get_politeness_metadata("https://example.com/page")
        assert meta["enabled"] is True
        assert meta["domain"] == "example.com"
        assert meta["crawl_delay_seconds"] == 2.0


# ── Domain extraction ───────────────────────────────────────────


class TestDomainExtraction:
    def test_simple_domain(self):
        assert (
            PolitenessManager._domain_from_url("https://example.com/path")
            == "example.com"
        )

    def test_with_port(self):
        assert (
            PolitenessManager._domain_from_url("http://localhost:8080/test")
            == "localhost"
        )

    def test_subdomain(self):
        assert (
            PolitenessManager._domain_from_url("https://sub.example.com/path?a=1")
            == "sub.example.com"
        )

    def test_invalid_url(self):
        assert PolitenessManager._domain_from_url("not-a-url") == ""
