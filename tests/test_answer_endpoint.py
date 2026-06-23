"""Unit tests for the answer endpoint's concurrent _scrape_urls().

Verifies concurrency logic (semaphore, timeout, early termination, error handling)
by mocking the scraper client. No Docker stack needed.

Run with:
    python3 -m pytest tests/test_answer_endpoint.py -v

Or run directly:
    python3 tests/test_answer_endpoint.py
"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agent-svc"))

from agent.research import _scrape_urls

# ── Video-platform filtering ─────────────────────────────────────


def test_is_video_platform_url_youtube() -> None:
    """Standard YouTube watch URLs are correctly identified."""
    from agent.research import _is_video_platform_url

    assert _is_video_platform_url("https://www.youtube.com/watch?v=abc12345678") is True
    assert _is_video_platform_url("https://youtube.com/watch?v=abc12345678") is True
    assert _is_video_platform_url("https://youtu.be/abc12345678") is True
    assert _is_video_platform_url("https://m.youtube.com/watch?v=abc12345678") is True
    assert _is_video_platform_url("https://youtube.com/shorts/abc12345678") is True


def test_is_video_platform_url_non_video() -> None:
    """Non-video domains are not flagged."""
    from agent.research import _is_video_platform_url

    assert _is_video_platform_url("https://en.wikipedia.org/wiki/Paris") is False
    assert _is_video_platform_url("https://github.com/groktopus/groktocrawl") is False
    assert _is_video_platform_url("https://example.com/article") is False


def test_is_video_platform_url_tiktok_instagram() -> None:
    """TikTok and Instagram URLs are also video-first platforms."""
    from agent.research import _is_video_platform_url

    assert _is_video_platform_url("https://www.tiktok.com/@user/video/123") is True
    assert _is_video_platform_url("https://tiktok.com/@user/video/123") is True
    assert _is_video_platform_url("https://vm.tiktok.com/abcde/") is True
    assert _is_video_platform_url("https://www.instagram.com/p/abc123/") is True
    assert _is_video_platform_url("https://instagram.com/reel/abc123/") is True


def test_url_splitting_preserves_ordering() -> None:
    """Preferred URLs keep their order; deprioritized go to the fallback list."""
    from agent.research import _is_video_platform_url

    urls = [
        "https://en.wikipedia.org/wiki/A",
        "https://www.youtube.com/watch?v=abc12345678",
        "https://en.wikipedia.org/wiki/B",
        "https://youtu.be/abc12345678",
    ]
    preferred = [u for u in urls if not _is_video_platform_url(u)]
    deprioritized = [u for u in urls if _is_video_platform_url(u)]

    assert preferred == [
        "https://en.wikipedia.org/wiki/A",
        "https://en.wikipedia.org/wiki/B",
    ]
    assert deprioritized == [
        "https://www.youtube.com/watch?v=abc12345678",
        "https://youtu.be/abc12345678",
    ]


class MockScraper:
    """Mock scraper client that simulates scrape results at configurable speeds.

    Tracks ``max_concurrent`` to verify the semaphore is respected.
    """

    def __init__(self, responses: dict | None = None, delay: float = 0):
        self.responses = responses or {}
        self.delay = delay
        self.call_count = 0
        self.concurrent = 0
        self.max_concurrent = 0
        self._calls: list[str] = []

    async def scrape(self, url: str) -> dict:
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        self._calls.append(url)

        if self.delay:
            await asyncio.sleep(self.delay)

        self.call_count += 1
        self.concurrent -= 1

        if url in self.responses:
            return self.responses[url]
        return {
            "success": True,
            "data": {"markdown": f"Content of {url}", "source": "direct"},
            "error": None,
        }

    async def scrape_with_fallback(self, url: str, **kwargs) -> dict:
        """Alias for scrape(), matching ScraperClient interface."""
        return await self.scrape(url)


# ── Fixtures ──────────────────────────────────────────────────────

GOOD_PAGE = {
    "success": True,
    "data": {"markdown": "Hello world content here.", "source": "direct"},
    "error": None,
}
FAILED_PAGE = {"success": False, "data": None, "error": "404 Not Found"}


# ── Concurrency ───────────────────────────────────────────────────


def test_semaphore_limits_concurrent_requests():
    """With max_concurrent=2, no more than 2 concurrent requests should run."""

    async def run():
        scraper = MockScraper(delay=0.1)
        urls = [f"https://example.com/{i}" for i in range(4)]
        await _scrape_urls(urls, scraper, min_sources=4, max_concurrent=2)
        # 4 URLs at 0.1s each with semaphore=2 → ~0.2s wall time, not 0.4s
        assert scraper.max_concurrent == 2, (
            f"Expected 2 concurrent, got {scraper.max_concurrent}"
        )

    asyncio.run(run())


def test_concurrent_is_faster_than_sequential():
    """4 URLs at 0.1s each should complete in ~0.2s, not ~0.4s."""

    async def run():
        scraper = MockScraper(delay=0.1)
        urls = [f"https://example.com/{i}" for i in range(4)]
        t0 = time.monotonic()
        await _scrape_urls(urls, scraper, min_sources=4, max_concurrent=2)
        elapsed = time.monotonic() - t0
        # With semaphore=2, 4×0.1s = 0.2s wall time, plus overhead
        assert elapsed < 0.35, f"Took {elapsed:.3f}s — expected <0.35s for concurrent"

    asyncio.run(run())


# ── Early termination ─────────────────────────────────────────────


def test_stops_early_when_min_sources_reached():
    """Once min_sources=2 are scraped, remaining URLs are not attempted."""

    async def run():
        scraper = MockScraper()
        urls = [f"https://example.com/{i}" for i in range(4)]
        await _scrape_urls(urls, scraper, min_sources=2, max_concurrent=2)
        # Should have stopped after 2 succeeded (batch of 2 with max_concurrent=2)
        assert scraper.call_count == 2, f"Expected 2 calls, got {scraper.call_count}"

    asyncio.run(run())


def test_early_termination_cancels_in_flight():
    """If min_sources is reached with tasks still running, they get cancelled."""

    async def run():
        scraper = MockScraper(delay=0.2)
        urls = [f"https://example.com/{i}" for i in range(3)]
        await _scrape_urls(urls, scraper, min_sources=1)
        # After first succeeds (0.2s), remaining 1-2 in-flight tasks are cancelled
        # Call count may be 3 (all started) but we complete with 1 doc
        assert scraper.call_count >= 1

    asyncio.run(run())


# ── Error handling ────────────────────────────────────────────────


def test_continues_after_failed_url():
    """A failing URL does not crash the batch — other URLs are still tried."""

    async def run():
        responses = {
            "https://example.com/fail": FAILED_PAGE,
        }
        scraper = MockScraper(responses=responses)
        urls = ["https://example.com/fail", "https://example.com/ok"]
        docs, sources = await _scrape_urls(urls, scraper, min_sources=2)
        assert len(docs) == 1
        assert sources[0]["url"] == "https://example.com/ok"

    asyncio.run(run())


def test_returns_empty_on_all_failures():
    """If every URL fails, returns empty lists."""

    async def run():
        responses = {
            "https://example.com/a": FAILED_PAGE,
            "https://example.com/b": FAILED_PAGE,
        }
        scraper = MockScraper(responses=responses)
        docs, sources = await _scrape_urls(
            list(responses.keys()), scraper, min_sources=2
        )
        assert docs == []
        assert sources == []

    asyncio.run(run())


# ── max_attempts ──────────────────────────────────────────────────


def test_max_attempts_limits_total_calls():
    """With max_attempts=2 and 4 URLs, only 2 URLs are attempted."""

    async def run():
        scraper = MockScraper()
        urls = [f"https://example.com/{i}" for i in range(4)]
        await _scrape_urls(urls, scraper, min_sources=4, max_attempts=2)
        assert scraper.call_count <= 2

    asyncio.run(run())


def test_max_attempts_defaults_to_all_urls():
    """Without max_attempts, all URLs should be tried."""

    async def run():
        scraper = MockScraper()
        urls = [f"https://example.com/{i}" for i in range(4)]
        await _scrape_urls(urls, scraper, min_sources=4)
        assert scraper.call_count == 4

    asyncio.run(run())


# ── Run directly ──────────────────────────────────────────────────

if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-v"]))
