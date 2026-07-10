"""Unit tests for playwright_retry — the bounded transient-retry helper."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure the local scraper-svc takes precedence over any installed version
_scraper_svc = Path(__file__).resolve().parent.parent.parent / "scraper-svc"
if str(_scraper_svc) not in sys.path:
    sys.path.insert(0, str(_scraper_svc))


async def _noop_sleep(_seconds: float) -> None:
    pass


@pytest.mark.asyncio
async def test_retry_transient_succeeds_after_failure(monkeypatch):
    from scraper.playwright_retry import retry_transient

    monkeypatch.setattr("scraper.playwright_retry.asyncio.sleep", _noop_sleep)

    calls = {"count": 0}

    async def flaky():
        calls["count"] += 1
        if calls["count"] == 1:
            raise Exception("page is navigating")
        return "ok"

    result = await retry_transient(flaky)
    assert result == "ok"
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_retry_transient_re_raises_non_matching(monkeypatch):
    from scraper.playwright_retry import retry_transient

    monkeypatch.setattr("scraper.playwright_retry.asyncio.sleep", _noop_sleep)

    calls = {"count": 0}

    async def broken():
        calls["count"] += 1
        raise RuntimeError("unrelated crash")

    with pytest.raises(RuntimeError, match="unrelated crash"):
        await retry_transient(broken)

    assert calls["count"] == 1


@pytest.mark.asyncio
async def test_retry_transient_re_raises_after_exhaustion(monkeypatch):
    from scraper.playwright_retry import retry_transient

    monkeypatch.setattr("scraper.playwright_retry.asyncio.sleep", _noop_sleep)

    calls = {"count": 0}

    async def always_navigating():
        calls["count"] += 1
        raise Exception("page is navigating")

    with pytest.raises(Exception, match="page is navigating"):
        await retry_transient(always_navigating)

    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_retry_transient_recognises_scrollheight(monkeypatch):
    from scraper.playwright_retry import retry_transient

    monkeypatch.setattr("scraper.playwright_retry.asyncio.sleep", _noop_sleep)

    calls = {"count": 0}

    async def null_body():
        calls["count"] += 1
        if calls["count"] < 3:
            raise Exception(
                "TypeError: Cannot read properties of null"
                " (reading 'scrollHeight')"
            )
        return "scrolled"

    result = await retry_transient(null_body)
    assert result == "scrolled"
    assert calls["count"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_message",
    [
        "page is navigating",
        "TypeError: Cannot read properties of null (reading 'scrollHeight')",
    ],
)
async def test_fetch_via_playwright_returns_browser_error_on_no_proxy_exhaustion(
    monkeypatch,
    error_message,
):
    """fetch_via_playwright returns error_type=browser_error when a
    no-proxy call exhausts retries on a known transient signature."""
    from scraper.fetch_tiers import fetch_via_playwright

    monkeypatch.setattr(
        "scraper.fetch_tiers._get_playwright_proxy",
        lambda: None,
    )

    async def _fail(_url, _proxy):
        raise Exception(error_message)

    monkeypatch.setattr(
        "scraper.fetch_tiers._playwright_fetch_with_proxy",
        _fail,
    )

    result = await fetch_via_playwright("http://example.com")
    assert result is not None
    assert result.get("error_type") == "browser_error"
    assert error_message in result.get("error", "")
