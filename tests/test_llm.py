"""Tests for agent-svc/agent/llm.py — LLMClient.

Tests message formatting, streaming, and error handling
using mocked HTTP responses.
"""

import os
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def llm():
    from agent.llm import LLMClient

    return LLMClient(
        base_url="http://llm.test/v1", api_key="test-key", model="test-model"
    )


class TestLLMClientInit:
    def test_strips_trailing_slash(self):
        from agent.llm import LLMClient

        client = LLMClient(base_url="http://example.com/v1/")
        assert client.base_url == "http://example.com/v1"

    def test_defaults(self):
        from agent.llm import LLMClient

        client = LLMClient()
        assert client.base_url == "https://api.openai.com/v1"
        assert client.api_key == ""
        assert client.model == "gpt-4o-mini"


def _make_response(status_code=200, json_data=None, text=""):
    """Create a mock httpx response for LLM generate()."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    return resp


class TestLLMClientGenerate:
    @pytest.mark.asyncio
    async def test_successful_generation(self, llm):
        with patch.object(
            llm._client,
            "post",
            return_value=_make_response(
                json_data={"choices": [{"message": {"content": "Hello world!"}}]}
            ),
        ):
            result = await llm.generate(
                system_prompt="Be helpful.", user_prompt="Say hi."
            )
            assert result == "Hello world!"

    @pytest.mark.asyncio
    async def test_includes_context_when_provided(self, llm):
        with patch.object(
            llm._client,
            "post",
            return_value=_make_response(
                json_data={"choices": [{"message": {"content": "Answer"}}]}
            ),
        ) as mock_post:
            result = await llm.generate(
                system_prompt="Be helpful.",
                user_prompt="What do you see?",
                context="The sky is blue.\n\nThe grass is green.",
            )
            assert result == "Answer"
            call_kwargs = mock_post.call_args[1]
            body = call_kwargs["json"]
            assert len(body["messages"]) == 2
            user_msg = body["messages"][1]
            assert "The sky is blue." in user_msg["content"]

    @pytest.mark.asyncio
    async def test_includes_schema_in_system_prompt(self, llm):
        schema = {"type": "object", "properties": {"key": {"type": "string"}}}
        with patch.object(
            llm._client,
            "post",
            return_value=_make_response(
                json_data={"choices": [{"message": {"content": '{"key": "value"}'}}]}
            ),
        ) as mock_post:
            result = await llm.generate(
                system_prompt="Extract data.",
                user_prompt="Extract from this.",
                schema=schema,
            )
            assert result == '{"key": "value"}'
            body = mock_post.call_args[1]["json"]
            assert body["response_format"] == {"type": "json_object"}
            assert "json" in body["messages"][0]["content"].lower()

    @pytest.mark.asyncio
    async def test_sets_authorization_header(self, llm):
        with patch.object(
            llm._client,
            "post",
            return_value=_make_response(
                json_data={"choices": [{"message": {"content": "ok"}}]}
            ),
        ) as mock_post:
            await llm.generate(system_prompt="x", user_prompt="y")
            headers = mock_post.call_args[1]["headers"]
            assert headers["Authorization"] == "Bearer test-key"

    @pytest.mark.asyncio
    async def test_no_auth_when_key_empty(self):
        from agent.llm import LLMClient

        no_key = LLMClient(base_url="http://test/v1", api_key="")
        with patch.object(
            no_key._client,
            "post",
            return_value=_make_response(
                json_data={"choices": [{"message": {"content": "ok"}}]}
            ),
        ) as mock_post:
            await no_key.generate(system_prompt="x", user_prompt="y")
            headers = mock_post.call_args[1]["headers"]
            assert "Authorization" not in headers

    @pytest.mark.asyncio
    async def test_handles_api_error(self, llm):
        with patch.object(
            llm._client,
            "post",
            return_value=_make_response(status_code=429, text="Rate limited"),
        ):
            result = await llm.generate(system_prompt="x", user_prompt="y")
            assert "Error: LLM API returned 429" in result

    @pytest.mark.asyncio
    async def test_handles_network_error(self, llm):
        import httpx

        with patch.object(
            llm._client, "post", side_effect=httpx.ConnectError("Connection refused")
        ):
            result = await llm.generate(system_prompt="x", user_prompt="y")
            assert "Error: LLM call failed" in result

    @pytest.mark.asyncio
    async def test_thinking_omitted_by_default(self, llm):
        with patch.object(
            llm._client,
            "post",
            return_value=_make_response(
                json_data={"choices": [{"message": {"content": "ok"}}]}
            ),
        ) as mock_post:
            await llm.generate(system_prompt="x", user_prompt="y")
            body = mock_post.call_args[1]["json"]
            assert "enable_thinking" not in body

    @pytest.mark.asyncio
    async def test_thinking_enabled_via_env(self):
        from agent.settings import load_settings as agent_load_settings

        agent_load_settings.cache_clear()
        with patch.dict(os.environ, {"LLM_ENABLE_THINKING": "true"}, clear=False):
            agent_load_settings.cache_clear()
            from agent.llm import LLMClient

            client = LLMClient(base_url="http://test/v1", api_key="k", model="ds")
            with patch.object(
                client._client,
                "post",
                return_value=_make_response(
                    json_data={"choices": [{"message": {"content": "ok"}}]}
                ),
            ) as mock_post:
                await client.generate(system_prompt="x", user_prompt="y")
                body = mock_post.call_args[1]["json"]
                assert body.get("enable_thinking") is True

    @pytest.mark.asyncio
    async def test_close(self, llm):
        with patch.object(llm._client, "aclose") as mock_close:
            await llm.close()
            mock_close.assert_called_once()


class TestLLMClientGenerateStream:
    @staticmethod
    def _setup_stream_mock(lines, status_code=200):
        """Create a properly nested mock for the httpx stream pattern."""

        async def async_iter():
            for line in lines:
                yield line

        async def async_read():
            return b"Error"

        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.aiter_lines = async_iter
        if status_code != 200:
            mock_resp.aread = async_read
        mock_resp.__aenter__.return_value = mock_resp
        mock_resp.__aexit__.return_value = None

        mock_client = MagicMock()
        mock_client.stream.return_value = mock_resp
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None

        mock_client_cls = MagicMock()
        mock_client_cls.return_value = mock_client

        return mock_client_cls, mock_client

    @pytest.mark.asyncio
    async def test_yields_tokens(self, llm):
        mock_client_cls, _ = self._setup_stream_mock(
            [
                'data: {"choices":[{"delta":{"content":"Hello"}}]}',
                'data: {"choices":[{"delta":{"content":" "}}]}',
                'data: {"choices":[{"delta":{"content":"world"}}]}',
                "data: [DONE]",
            ]
        )

        with patch("httpx.AsyncClient", mock_client_cls):
            tokens = []
            async for event in llm.generate_stream(system_prompt="x", user_prompt="y"):
                tokens.append(event)

            assert len(tokens) == 4
            assert tokens[0] == {"type": "token", "content": "Hello"}
            assert tokens[1] == {"type": "token", "content": " "}
            assert tokens[2] == {"type": "token", "content": "world"}
            assert tokens[3]["type"] == "done"
            assert "Hello world" in tokens[3]["full_content"]

    @pytest.mark.asyncio
    async def test_yields_error_on_non_200(self, llm):
        mock_client_cls, _ = self._setup_stream_mock([], status_code=500)

        with patch("httpx.AsyncClient", mock_client_cls):
            events = []
            async for event in llm.generate_stream(system_prompt="x", user_prompt="y"):
                events.append(event)
            assert len(events) == 1
            assert events[0]["type"] == "error"
            assert "500" in events[0]["content"]

    @pytest.mark.asyncio
    async def test_yields_error_on_exception(self, llm):
        import httpx

        mock_client = MagicMock()
        mock_client.stream.side_effect = httpx.ConnectError("timeout")
        mock_client.__aenter__.return_value = mock_client

        mock_client_cls = MagicMock()
        mock_client_cls.return_value = mock_client

        with patch("httpx.AsyncClient", mock_client_cls):
            events = []
            async for event in llm.generate_stream(system_prompt="x", user_prompt="y"):
                events.append(event)
            assert len(events) == 1
            assert events[0]["type"] == "error"

    @pytest.mark.asyncio
    async def test_skips_invalid_json_lines(self, llm):
        mock_client_cls, _ = self._setup_stream_mock(
            [
                "data: invalid json",
                'data: {"choices":[{"delta":{"content":"ok"}}]}',
                "data: [DONE]",
            ]
        )

        with patch("httpx.AsyncClient", mock_client_cls):
            tokens = []
            async for event in llm.generate_stream(system_prompt="x", user_prompt="y"):
                tokens.append(event)
            assert len(tokens) == 2
            assert tokens[0] == {"type": "token", "content": "ok"}

    @pytest.mark.asyncio
    async def test_includes_context(self, llm):
        mock_client_cls, mock_client = self._setup_stream_mock(
            [
                'data: {"choices":[{"delta":{"content":"answer"}}]}',
                "data: [DONE]",
            ]
        )

        with patch("httpx.AsyncClient", mock_client_cls):
            async for _event in llm.generate_stream(
                system_prompt="x", user_prompt="what?", context="Some context here."
            ):
                pass

            body = mock_client.stream.call_args[1]["json"]
            user_msg = body["messages"][1]["content"]
            assert "Some context here." in user_msg
