"""Tests for the groktocrawl CLI — crawl subcommand flags and error handling.

Covers:
- Client.crawl() parameter mapping to API
- cmd_crawl() handler behavior with new flags
- JSON output formatting
- Server-unreachable error handling
- Help text completeness
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Load the CLI script ──────────────────────────────────────────────────────

_CLI_PATH = os.path.join(os.path.dirname(__file__), "..", "groktocrawl")
if not os.path.isfile(_CLI_PATH):
    pytest.skip("groktocrawl CLI not found at project root", allow_module_level=True)

# Load the CLI module by executing it with a namespace dict
_cli_ns: dict = {}
with open(_CLI_PATH, encoding="utf-8") as f:
    _code = compile(f.read(), _CLI_PATH, "exec")
exec(_code, _cli_ns)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    """Create a Client instance with dry_run=False for testing."""
    client_cls = _cli_ns["Client"]
    return client_cls(server="http://test-server:8080", dry_run=False)


# ── Client.crawl() tests ─────────────────────────────────────────────────────


class TestClientCrawl:
    """Tests for Client.crawl() parameter mapping."""

    def test_basic_crawl_sends_correct_data(self, client):
        """Basic crawl sends url, limit, and max_depth."""

        def _fake_request(method, path, json_data=None, params=None):
            assert json_data["url"] == "http://example.com"
            assert json_data["limit"] == 50
            assert json_data["max_depth"] == 2
            return {"success": True, "id": "crawl-1234-uuid"}

        client._request = _fake_request
        result = client.crawl(url="http://example.com")
        assert result["id"] == "crawl-1234-uuid"

    def test_crawl_sends_include_paths(self, client):
        """include_paths is passed as include_paths."""

        def _fake_request(method, path, json_data=None, params=None):
            assert json_data["include_paths"] == ["/blog/*", "/docs/*"]
            return {"success": True, "id": "job-1"}

        client._request = _fake_request
        result = client.crawl(
            url="http://example.com",
            include_paths=["/blog/*", "/docs/*"],
        )
        assert result["id"] == "job-1"

    def test_crawl_sends_exclude_paths(self, client):
        """exclude_paths is passed as exclude_paths."""

        def _fake_request(method, path, json_data=None, params=None):
            assert json_data["exclude_paths"] == ["/admin/*"]
            return {"success": True, "id": "job-1"}

        client._request = _fake_request
        result = client.crawl(
            url="http://example.com",
            exclude_paths=["/admin/*"],
        )
        assert result["id"] == "job-1"

    def test_crawl_sends_max_pages(self, client):
        """max_pages is passed as max_pages."""

        def _fake_request(method, path, json_data=None, params=None):
            assert json_data["max_pages"] == 5
            return {"success": True, "id": "job-1"}

        client._request = _fake_request
        result = client.crawl(
            url="http://example.com",
            max_pages=5,
        )
        assert result["id"] == "job-1"

    def test_crawl_sends_max_depth(self, client):
        """max_depth is passed as max_depth."""

        def _fake_request(method, path, json_data=None, params=None):
            assert json_data["max_depth"] == 1
            return {"success": True, "id": "job-1"}

        client._request = _fake_request
        result = client.crawl(
            url="http://example.com",
            max_depth=1,
        )
        assert result["id"] == "job-1"

    def test_crawl_sends_ignore_query_parameters(self, client):
        """ignore_query_parameters=True sends ignore_query_parameters."""

        def _fake_request(method, path, json_data=None, params=None):
            assert json_data["ignore_query_parameters"] is True
            return {"success": True, "id": "job-1"}

        client._request = _fake_request
        result = client.crawl(
            url="http://example.com",
            ignore_query_parameters=True,
        )
        assert result["id"] == "job-1"

    def test_crawl_does_not_send_ignore_query_parameters_by_default(self, client):
        """ignore_query_parameters=False does not send the field."""

        def _fake_request(method, path, json_data=None, params=None):
            assert "ignore_query_parameters" not in json_data
            return {"success": True, "id": "job-1"}

        client._request = _fake_request
        result = client.crawl(
            url="http://example.com",
            ignore_query_parameters=False,
        )
        assert result["id"] == "job-1"

    def test_crawl_does_not_send_max_pages_when_none(self, client):
        """max_pages=None does not send max_pages."""

        def _fake_request(method, path, json_data=None, params=None):
            assert "max_pages" not in json_data
            return {"success": True, "id": "job-1"}

        client._request = _fake_request
        result = client.crawl(
            url="http://example.com",
            max_pages=None,
        )
        assert result["id"] == "job-1"

    def test_crawl_sends_both_limit_and_max_pages(self, client):
        """Both limit and max_pages can be sent together."""

        def _fake_request(method, path, json_data=None, params=None):
            assert json_data["limit"] == 50
            assert json_data["max_pages"] == 5
            return {"success": True, "id": "job-1"}

        client._request = _fake_request
        result = client.crawl(
            url="http://example.com",
            limit=50,
            max_pages=5,
        )
        assert result["id"] == "job-1"


# ── Connection error handling ────────────────────────────────────────────────


class TestConnectionErrorHandling:
    """Tests that connection errors produce clean messages without tracebacks."""

    def test_connection_error_is_caught_and_clean(self, client):
        """ConnectionError in _request raises ApiError with clean message."""
        api_error_cls = _cli_ns["ApiError"]
        with patch.object(
            client,
            "_request",
            side_effect=api_error_cls(
                "Cannot connect to http://test-server:8080: "
                "ConnectionRefusedError(61, 'Connection refused')\n"
                "  Is the server running? Set --server or GROKTOCRAWL_API_URL"
            ),
        ):
            with pytest.raises(api_error_cls) as exc_info:
                client.crawl(url="http://example.com")
            msg = str(exc_info.value)
            assert "Cannot connect" in msg
            assert "Is the server running" in msg
            assert "Traceback" not in msg

    def test_cmd_crawl_connection_error_exits_nonzero(self):
        """cmd_crawl exits non-zero with clean error message on connection error."""
        api_error_cls = _cli_ns["ApiError"]
        cmd_crawl = _cli_ns["cmd_crawl"]

        mock_args = MagicMock()
        mock_args.url = "http://example.com"
        mock_args.limit = 50
        mock_args.max_depth = 2
        mock_args.include_paths = None
        mock_args.exclude_paths = None
        mock_args.max_pages = None
        mock_args.ignore_query_parameters = False
        mock_args.no_poll = True
        mock_args.dry_run = False

        mock_client = MagicMock()
        mock_client.dry_run = False
        mock_client.crawl.side_effect = api_error_cls(
            "Cannot connect to http://localhost:8080: Connection refused"
        )

        stderr = _capture_stderr(cmd_crawl, mock_client, mock_args)
        assert "Error:" in stderr
        assert "Cannot connect" in stderr
        assert "Traceback" not in stderr


# ── cmd_crawl handler tests ──────────────────────────────────────────────────


class TestCmdCrawl:
    """Tests for the cmd_crawl() handler function."""

    def test_cmd_crawl_sends_new_flags(self):
        """cmd_crawl passes max_pages and ignore_query_parameters to client.crawl()."""
        cmd_crawl = _cli_ns["cmd_crawl"]

        mock_args = MagicMock()
        mock_args.url = "http://example.com"
        mock_args.limit = 50
        mock_args.max_depth = 2
        mock_args.include_paths = None
        mock_args.exclude_paths = None
        mock_args.max_pages = 5
        mock_args.ignore_query_parameters = True
        mock_args.no_poll = True
        mock_args.dry_run = False

        mock_client = MagicMock()
        mock_client.dry_run = False
        mock_client.crawl.return_value = {"success": True, "id": "job-1"}

        # With no_poll=True, cmd_crawl returns normally after printing job ID
        cmd_crawl(mock_client, mock_args)

        mock_client.crawl.assert_called_once_with(
            url="http://example.com",
            limit=50,
            max_depth=2,
            include_paths=None,
            exclude_paths=None,
            max_pages=5,
            ignore_query_parameters=True,
        )

    def test_cmd_crawl_json_output(self):
        """cmd_crawl with JSON_OUTPUT produces valid JSON."""
        cmd_crawl = _cli_ns["cmd_crawl"]

        mock_args = MagicMock()
        mock_args.url = "http://example.com"
        mock_args.limit = 1
        mock_args.max_depth = 2
        mock_args.include_paths = None
        mock_args.exclude_paths = None
        mock_args.max_pages = None
        mock_args.ignore_query_parameters = False
        mock_args.dry_run = False
        mock_args.no_poll = False

        mock_client = MagicMock()
        mock_client.dry_run = False
        mock_client.crawl.return_value = {"success": True, "id": "job-json-test"}
        mock_client.crawl_status.return_value = {
            "success": True,
            "status": "completed",
            "completed": 2,
            "total": 2,
            "data": [
                {"url": "http://example.com/page1", "markdown": "# Page 1"},
                {"url": "http://example.com/page2", "markdown": "# Page 2"},
            ],
        }

        original_json = _cli_ns["JSON_OUTPUT"]
        _cli_ns["JSON_OUTPUT"] = True
        try:
            stdout = _capture_stdout(cmd_crawl, mock_client, mock_args)
        finally:
            _cli_ns["JSON_OUTPUT"] = original_json

        output = stdout.strip()
        # Output should be valid JSON
        parsed = json.loads(output)
        assert parsed["job_id"] == "job-json-test"
        assert parsed["status"] == "completed"
        assert len(parsed["pages"]) == 2

    def test_cmd_crawl_polling_error_retry_limit(self):
        """cmd_crawl exits after max polling errors."""
        cmd_crawl = _cli_ns["cmd_crawl"]
        api_error_cls = _cli_ns["ApiError"]

        mock_args = MagicMock()
        mock_args.url = "http://example.com"
        mock_args.limit = 50
        mock_args.max_depth = 2
        mock_args.include_paths = None
        mock_args.exclude_paths = None
        mock_args.max_pages = None
        mock_args.ignore_query_parameters = False
        mock_args.dry_run = False
        mock_args.no_poll = False

        mock_client = MagicMock()
        mock_client.dry_run = False
        mock_client.crawl.return_value = {"success": True, "id": "job-poll-err"}
        # All status checks fail
        mock_client.crawl_status.side_effect = api_error_cls(
            "Status check failed: connection lost"
        )

        stderr = _capture_stderr(cmd_crawl, mock_client, mock_args)
        assert "Lost connection" in stderr
        assert "Traceback" not in stderr


def _capture_stdout(func, *args, **kwargs):
    """Run func and capture stdout, returning the string."""
    from contextlib import redirect_stdout, suppress
    from io import StringIO

    buf = StringIO()
    with redirect_stdout(buf), suppress(SystemExit):
        func(*args, **kwargs)
    return buf.getvalue()


def _capture_stderr(func, *args, **kwargs):
    """Run func and capture stderr, returning the string."""
    from contextlib import redirect_stderr, suppress
    from io import StringIO

    buf = StringIO()
    with redirect_stderr(buf), suppress(SystemExit):
        func(*args, **kwargs)
    return buf.getvalue()


# ── Parser / help text tests ─────────────────────────────────────────────────


class TestCrawlParser:
    """Tests for the crawl subcommand argument parser."""

    def test_help_contains_crawl_flags(self):
        """crawl --help lists all flags including new ones."""
        make_parser = _cli_ns["make_parser"]
        parser = make_parser()

        from contextlib import redirect_stdout
        from io import StringIO

        stdout = StringIO()
        with pytest.raises(SystemExit):
            with redirect_stdout(stdout):
                parser.parse_args(["crawl", "--help"])
        help_text = stdout.getvalue()

        # Existing flags
        assert "--limit" in help_text
        assert "--max-depth" in help_text
        assert "--include-paths" in help_text
        assert "--exclude-paths" in help_text
        assert "--no-poll" in help_text

        # New flags
        assert "--max-pages" in help_text
        assert "--ignore-query-params" in help_text

    def test_parse_crawl_args_max_pages(self):
        """--max-pages is parsed correctly."""
        make_parser = _cli_ns["make_parser"]
        parser = make_parser()
        args = parser.parse_args(["crawl", "http://example.com", "--max-pages", "5"])
        assert args.max_pages == 5

    def test_parse_crawl_args_max_pages_default_none(self):
        """--max-pages defaults to None."""
        make_parser = _cli_ns["make_parser"]
        parser = make_parser()
        args = parser.parse_args(["crawl", "http://example.com"])
        assert args.max_pages is None

    def test_parse_crawl_args_ignore_query_params(self):
        """--ignore-query-params is parsed correctly."""
        make_parser = _cli_ns["make_parser"]
        parser = make_parser()
        args = parser.parse_args(
            ["crawl", "http://example.com", "--ignore-query-params"]
        )
        assert args.ignore_query_parameters is True

    def test_parse_crawl_args_ignore_query_params_default(self):
        """--ignore-query-params defaults to False."""
        make_parser = _cli_ns["make_parser"]
        parser = make_parser()
        args = parser.parse_args(["crawl", "http://example.com"])
        assert args.ignore_query_parameters is False

    def test_parse_crawl_args_include_paths(self):
        """--include-paths parses multiple path patterns."""
        make_parser = _cli_ns["make_parser"]
        parser = make_parser()
        args = parser.parse_args(
            [
                "crawl",
                "http://example.com",
                "--include-paths",
                "/blog/*",
                "/docs/*",
            ]
        )
        assert args.include_paths == ["/blog/*", "/docs/*"]

    def test_parse_crawl_args_exclude_paths(self):
        """--exclude-paths parses multiple path patterns."""
        make_parser = _cli_ns["make_parser"]
        parser = make_parser()
        args = parser.parse_args(
            [
                "crawl",
                "http://example.com",
                "--exclude-paths",
                "/admin/*",
            ]
        )
        assert args.exclude_paths == ["/admin/*"]

    def test_parse_crawl_args_max_depth(self):
        """--max-depth is parsed correctly."""
        make_parser = _cli_ns["make_parser"]
        parser = make_parser()
        args = parser.parse_args(["crawl", "http://example.com", "--max-depth", "1"])
        assert args.max_depth == 1

    def test_parse_crawl_args_limit(self):
        """--limit is parsed correctly."""
        make_parser = _cli_ns["make_parser"]
        parser = make_parser()
        args = parser.parse_args(["crawl", "http://example.com", "--limit", "10"])
        assert args.limit == 10

    def test_global_help_shows_crawl(self):
        """Top-level --help lists the crawl subcommand."""
        make_parser = _cli_ns["make_parser"]
        parser = make_parser()

        from contextlib import redirect_stdout
        from io import StringIO

        stdout = StringIO()
        with pytest.raises(SystemExit):
            with redirect_stdout(stdout):
                parser.parse_args(["--help"])
        help_text = stdout.getvalue()
        assert "crawl" in help_text


# ── Subprocess integration tests ──────────────────────────────────────────────


class TestCLISubprocess:
    """Integration tests that run the CLI as a subprocess."""

    def test_help_shows_crawl_flags(self):
        """Running groktocrawl crawl --help shows all flags."""
        result = subprocess.run(
            [sys.executable, _CLI_PATH, "crawl", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        # Old flags
        assert "--limit" in result.stdout
        assert "--max-depth" in result.stdout
        assert "--include-paths" in result.stdout
        assert "--exclude-paths" in result.stdout
        assert "--no-poll" in result.stdout
        # New flags
        assert "--max-pages" in result.stdout
        assert "--ignore-query-params" in result.stdout

    def test_top_level_help_shows_crawl(self):
        """Top-level --help lists crawl command."""
        result = subprocess.run(
            [sys.executable, _CLI_PATH, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert "crawl" in result.stdout
