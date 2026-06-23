"""Tests for portal-svc — web portal endpoints.

Integration-level tests using FastAPI's TestClient.  Unit-level proxy-logic
tests live in ``test_portal_svc_unit.py``.
"""

import re
import pytest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from portal.app import app

client = TestClient(app)


# ── Helper ───────────────────────────────────────────────────────────────────


def _mock_async_client(status_code: int = 200, chunks: list[bytes] | None = None):
    """Build a mocked ``httpx.AsyncClient`` for use in integration tests."""
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.__aenter__.return_value = mock_client

    mock_response = AsyncMock(spec=httpx.Response)
    mock_response.status_code = status_code
    mock_response.aread = AsyncMock(return_value=b'{"detail":"error"}')

    async def _aiter_bytes():
        for c in chunks or []:
            yield c

    mock_response.aiter_bytes = _aiter_bytes

    stream_cm = AsyncMock()
    stream_cm.__aenter__.return_value = mock_response
    mock_client.stream.return_value = stream_cm

    return mock_client


# ── Health ───────────────────────────────────────────────────────────────────


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["service"] == "portal-svc"


# ── Metrics (including portal_queries_total) ────────────────────────────────


def test_metrics_openmetrics_format():
    """/metrics returns valid OpenMetrics text."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "openmetrics-text" in resp.headers["content-type"]
    body = resp.text
    assert "# HELP" in body or body.strip() == "# EOF\n"


def test_metrics_contains_portal_queries_total():
    """portal_queries_total counter is present in /metrics."""
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "portal_queries_total" in resp.text


def test_portal_queries_total_increments():
    """Each /ask POST increments portal_queries_total."""
    # Read baseline
    resp_before = client.get("/metrics")
    before_text = resp_before.text
    match_before = re.search(r"portal_queries_total\s+([\d.]+)", before_text)
    before_val = float(match_before.group(1)) if match_before else 0.0

    # Trigger a /ask (successful or not, the counter still increments)
    client.post("/ask", data={"query": "inc test", "num_sources": "1"})

    # Read after
    resp_after = client.get("/metrics")
    after_text = resp_after.text
    match_after = re.search(r"portal_queries_total\s+([\d.]+)", after_text)
    after_val = float(match_after.group(1)) if match_after else 0.0

    assert after_val == before_val + 1.0, (
        f"Expected portal_queries_total to increment by 1 ({before_val} -> {after_val})"
    )


# ── Index ────────────────────────────────────────────────────────────────────


def test_index_returns_html():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<html" in resp.text.lower()


# ── /ask — basic ────────────────────────────────────────────────────────────


@pytest.mark.xfail(strict=False, reason="portal timeout on self-hosted runner")
def test_ask_endpoint_accepts_post():
    resp = client.post("/ask", data={"query": "test", "num_sources": "3"})
    assert resp.status_code in (200, 502, 503)


def test_ask_endpoint_content_type_event_stream():
    """When the proxy succeeds, Content-Type is text/event-stream."""
    chunks = [b"data: ok\n\n"]
    mock_client = _mock_async_client(status_code=200, chunks=chunks)

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        resp = client.post("/ask", data={"query": "sse"})

    assert resp.status_code == 200
    ct = resp.headers.get("content-type", "")
    assert ct.startswith("text/event-stream")


def test_ask_sse_chunks_forwarded():
    """SSE chunks from the downstream agent are forwarded to the caller."""
    chunks = [b"event: token\ndata: Hello\n\n", b"event: done\ndata: {}\n\n"]
    mock_client = _mock_async_client(status_code=200, chunks=chunks)

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        resp = client.post("/ask", data={"query": "sse"})

    assert resp.status_code == 200
    assert "event: token" in resp.text
    assert "event: done" in resp.text
    assert "Hello" in resp.text


# ── /ask — empty query ──────────────────────────────────────────────────────


def test_ask_empty_query():
    """POST /ask with an empty query string does not crash (may return 422)."""
    resp = client.post("/ask", data={"query": "", "num_sources": "3"})
    # The portal must not crash; 422 is acceptable if validation rejects
    # empty strings, and 200/502/503 are the nominal paths.
    assert resp.status_code in (200, 422, 502, 503)


def test_ask_empty_query_sse_stream():
    """Empty query with mocked downstream (or 422 from validation)."""
    chunks = [b"event: done\ndata: {}\n\n"]
    mock_client = _mock_async_client(status_code=200, chunks=chunks)

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        resp = client.post("/ask", data={"query": "", "num_sources": "3"})

    # FastAPI validation may reject empty query (422) before reaching the
    # proxy; the server must not crash regardless.
    assert resp.status_code in (200, 422)
    if resp.status_code == 200:
        assert resp.headers.get("content-type", "").startswith("text/event-stream")


# ── /ask — large num_sources ────────────────────────────────────────────────


def test_ask_large_num_sources():
    """POST /ask with num_sources=100 does not crash."""
    resp = client.post("/ask", data={"query": "test", "num_sources": "100"})
    assert resp.status_code in (200, 502, 503)


def test_ask_large_num_sources_forwarded_properly():
    """A large num_sources value is sent as integer in the proxy JSON."""
    chunks = [b"data: ok\n\n"]
    mock_client = _mock_async_client(status_code=200, chunks=chunks)

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        resp = client.post("/ask", data={"query": "large", "num_sources": "100"})

    assert resp.status_code == 200
    mock_client.stream.assert_called_once()
    call_json = mock_client.stream.call_args[1]["json"]
    assert call_json["num_sources"] == 100
    assert isinstance(call_json["num_sources"], int)


# ── /ask — downstream errors ────────────────────────────────────────────────


def test_ask_downstream_error_returns_sse():
    """Non-2xx from downstream is returned as SSE error event (not crash)."""
    mock_client = _mock_async_client(status_code=503)

    with patch.object(httpx, "AsyncClient", return_value=mock_client):
        resp = client.post("/ask", data={"query": "err"})

    assert resp.status_code == 200
    assert "event: error" in resp.text
