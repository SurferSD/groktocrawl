"""Tests for agent-svc/agent/models.py — Pydantic request/response models."""

import json

import pytest
from pydantic import ValidationError


class TestScrapeRequest:
    def test_minimal(self):
        from agent.models import ScrapeRequest

        r = ScrapeRequest(url="https://example.com")
        assert r.url == "https://example.com"
        assert r.formats == ["markdown"]
        assert r.only_main_content is True
        assert r.timeout == 30000

    def test_with_options(self):
        from agent.models import ScrapeRequest

        r = ScrapeRequest(
            url="https://example.com", formats=["markdown", "html"], timeout=10000
        )
        assert r.formats == ["markdown", "html"]
        assert r.timeout == 10000


class TestScrapeData:
    def test_defaults(self):
        from agent.models import ScrapeData

        d = ScrapeData()
        assert d.markdown == ""
        assert d.metadata == {}
        assert d.download is None
        assert d.quality is None


class TestScrapeResponse:
    def test_minimal(self):
        from agent.models import ScrapeResponse

        r = ScrapeResponse(success=True)
        assert r.success is True
        assert r.data is None
        assert r.error is None

    def test_with_data(self):
        from agent.models import ScrapeData, ScrapeResponse

        d = ScrapeData(markdown="# Hello")
        r = ScrapeResponse(success=True, data=d)
        assert r.data.markdown == "# Hello"


class TestAgentRequest:
    def test_required_fields(self):
        from agent.models import AgentRequest

        r = AgentRequest(prompt="Research AI")
        assert r.prompt == "Research AI"
        assert r.model == "default"
        assert r.stream is False

    def test_schema_alias(self):
        from agent.models import AgentRequest

        r = AgentRequest(prompt="Test", schema={"type": "object"})
        assert r.schema_ == {"type": "object"}

    def test_serializes_correctly(self):
        from agent.models import AgentRequest

        r = AgentRequest(prompt="Hello", urls=["https://a.com"])
        d = r.model_dump(by_alias=True)
        assert d["prompt"] == "Hello"
        assert d["urls"] == ["https://a.com"]

    def test_rejects_too_long_prompt(self):
        from agent.models import AgentRequest

        with pytest.raises(ValidationError):
            AgentRequest(prompt="x" * 100001)


class TestAgentCreateResponse:
    def test_minimal(self):
        from agent.models import AgentCreateResponse

        r = AgentCreateResponse(id="abc-123")
        assert r.success is True
        assert r.id == "abc-123"


class TestAgentStatusResponse:
    def test_defaults(self):
        from agent.models import AgentStatusResponse

        r = AgentStatusResponse()
        assert r.status == "processing"
        assert r.data is None

    def test_with_data(self):
        from agent.models import AgentStatusResponse

        r = AgentStatusResponse(status="completed", data={"result": "done"})
        assert r.data == {"result": "done"}


class TestSearchRequest:
    def test_defaults(self):
        from agent.models import SearchRequest

        r = SearchRequest(query="test query")
        assert r.limit == 5
        assert r.search_type == "fast"

    def test_with_categories(self):
        from agent.models import SearchRequest

        r = SearchRequest(query="test", categories=["news", "science"])
        assert r.categories == ["news", "science"]

    def test_with_output_schema(self):
        from agent.models import SearchRequest

        r = SearchRequest(query="test", output_schema={"type": "object"})
        assert r.output_schema == {"type": "object"}


class TestSearchResult:
    def test_minimal(self):
        from agent.models import SearchResult

        r = SearchResult(url="https://x.com", title="X")
        assert r.description == ""


class TestSearchResponse:
    def test_defaults(self):
        from agent.models import SearchResponse

        r = SearchResponse()
        assert r.success is True
        assert r.data["web"] == []


class TestSource:
    def test_minimal(self):
        from agent.models import Source

        s = Source(url="https://x.com")
        assert s.title == ""
        assert s.relevance == ""


class TestAnswerRequest:
    def test_minimal(self):
        from agent.models import AnswerRequest

        r = AnswerRequest(query="What is AI?")
        assert r.num_sources == 5
        assert r.stream is False

    def test_defaults(self):
        from agent.models import AnswerRequest

        r = AnswerRequest(query="test")
        assert r.search_type == "auto"
        assert r.retrieval_mode == "keyword"

    def test_rejects_long_query(self):
        from agent.models import AnswerRequest

        with pytest.raises(ValidationError):
            AnswerRequest(query="x" * 10001)


class TestAnswerResponse:
    def test_defaults(self):
        from agent.models import AnswerResponse

        r = AnswerResponse()
        assert r.success is True
        assert r.answer == ""
        assert r.sources == []

    def test_with_citations(self):
        from agent.models import AnswerResponse, Citation

        r = AnswerResponse(
            answer="Yes", citations=[Citation(index=1, url="https://x.com")]
        )
        assert len(r.citations) == 1
        assert r.citations[0].index == 1


class TestCrawlRequest:
    def test_minimal(self):
        from agent.models import CrawlRequest

        r = CrawlRequest(url="https://example.com")
        assert r.max_pages == 0
        assert r.max_depth == 2
        assert r.regex_on_full_url is False
        assert r.verbose is False

    def test_with_webhook(self):
        from agent.models import CrawlRequest

        r = CrawlRequest(
            url="https://x.com", webhook={"url": "https://hook.example.com"}
        )
        assert r.webhook["url"] == "https://hook.example.com"

    def test_with_regex_on_full_url(self):
        from agent.models import CrawlRequest

        r = CrawlRequest(
            url="https://example.com",
            regex_on_full_url=True,
            include_paths=[r"/section/.*"],
        )
        assert r.regex_on_full_url is True
        assert r.include_paths == [r"/section/.*"]

    def test_invalid_regex_raises_validation_error(self):
        from agent.models import CrawlRequest

        with pytest.raises(ValidationError) as excinfo:
            CrawlRequest(
                url="https://example.com",
                regex_on_full_url=True,
                include_paths=["[unclosed"],
            )
        err = str(excinfo.value)
        assert "include_paths" in err
        assert "unclosed" in err.lower() or "invalid" in err.lower()

    def test_invalid_regex_in_exclude_paths_raises_error(self):
        from agent.models import CrawlRequest

        with pytest.raises(ValidationError) as excinfo:
            CrawlRequest(
                url="https://example.com",
                regex_on_full_url=True,
                exclude_paths=[r"[\w+", r"/valid/\d+"],
            )
        err = str(excinfo.value)
        assert "exclude_paths" in err

    def test_valid_regex_passes_validation(self):
        from agent.models import CrawlRequest

        r = CrawlRequest(
            url="https://example.com",
            regex_on_full_url=True,
            include_paths=[r"/section/\d+", r"/blog/.*"],
            exclude_paths=[r"/admin/.*"],
        )
        assert r.include_paths == [r"/section/\d+", r"/blog/.*"]
        assert r.exclude_paths == [r"/admin/.*"]

    def test_empty_include_paths_with_regex_is_valid(self):
        from agent.models import CrawlRequest

        r = CrawlRequest(
            url="https://example.com",
            regex_on_full_url=True,
            include_paths=[],
        )
        assert r.include_paths == []

    def test_verbose_flag(self):
        from agent.models import CrawlRequest

        r = CrawlRequest(url="https://example.com", verbose=True)
        assert r.verbose is True


class TestBatchScrapeRequest:
    def test_minimal(self):
        from agent.models import BatchScrapeRequest

        r = BatchScrapeRequest(urls=["https://a.com", "https://b.com"])
        assert r.max_concurrency == 3


class TestActivityItem:
    def test_minimal(self):
        from agent.models import ActivityItem

        r = ActivityItem(
            id="abc",
            kind="agent",
            status="processing",
            created_at="2026-01-01T00:00:00",
        )
        assert r.url is None
        assert r.completed_at is None

    def test_full(self):
        from agent.models import ActivityItem

        r = ActivityItem(
            id="abc",
            kind="crawl",
            status="completed",
            url="https://x.com",
            created_at="2026-01-01T00:00:00",
            completed_at="2026-01-01T00:01:00",
        )
        assert r.completed_at == "2026-01-01T00:01:00"


class TestActivityResponse:
    def test_defaults(self):
        from agent.models import ActivityResponse

        r = ActivityResponse()
        assert r.data == []


class TestMapRequest:
    def test_minimal(self):
        from agent.models import MapRequest

        r = MapRequest(url="https://example.com")
        assert r.limit == 100
        assert r.search is None


class TestExtractRequest:
    def test_minimal(self):
        from agent.models import ExtractRequest

        r = ExtractRequest(urls=["https://a.com"])
        assert r.prompt is None
        assert r.model == "default"

    def test_schema_alias(self):
        from agent.models import ExtractRequest

        r = ExtractRequest(urls=["https://a.com"], schema={"type": "object"})
        assert r.schema_ == {"type": "object"}

    def test_rejects_empty_urls(self):
        from agent.models import ExtractRequest

        with pytest.raises(ValidationError):
            ExtractRequest(urls=[])


class TestMonitorCreateRequest:
    def test_minimal(self):
        from agent.models import MonitorCreateRequest

        r = MonitorCreateRequest(url="https://x.com")
        assert r.schedule == "0 */6 * * *"


class TestLLMsTextRequest:
    def test_minimal(self):
        from agent.models import LLMsTextRequest

        r = LLMsTextRequest(url="https://x.com")
        assert r.max_pages == 50

    def test_clamps_max_pages(self):
        from agent.models import LLMsTextRequest

        with pytest.raises(ValidationError):
            LLMsTextRequest(url="https://x.com", max_pages=600)


class TestAnswerRequestFull:
    def test_serialization_roundtrip(self):
        from agent.models import AnswerRequest

        r = AnswerRequest(query="test query", num_sources=10, model="gpt-4o")
        d = r.model_dump()
        assert d["query"] == "test query"
        assert d["num_sources"] == 10
        assert d["model"] == "gpt-4o"

    def test_deserialize_from_json(self):
        from agent.models import AnswerRequest

        payload = '{"query": "test", "num_sources": 3, "stream": true}'
        parsed = json.loads(payload)
        r = AnswerRequest(**parsed)
        assert r.stream is True
        assert r.num_sources == 3


class TestBrowserModels:
    def test_browser_create_request(self):
        from agent.models import BrowserCreateRequest

        r = BrowserCreateRequest()
        assert r.ttl == 300

    def test_browser_create_request_ttl_bounds(self):
        from agent.models import BrowserCreateRequest

        with pytest.raises(ValidationError):
            BrowserCreateRequest(ttl=10)  # below ge=30
        with pytest.raises(ValidationError):
            BrowserCreateRequest(ttl=5000)  # above le=3600

    def test_browser_execute_request(self):
        from agent.models import BrowserExecuteRequest

        r = BrowserExecuteRequest(action="navigate", url="https://x.com")
        assert r.timeout == 10000

    def test_browser_execute_response(self):
        from agent.models import BrowserExecuteResponse

        r = BrowserExecuteResponse(success=True, result="html content")
        assert r.result == "html content"
