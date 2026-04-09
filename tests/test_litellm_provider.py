from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nanobot.providers.litellm_provider import LiteLLMProvider


def _tool_call(name: str, arguments: str):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def test_parse_response_merges_tool_calls_across_choices() -> None:
    provider = LiteLLMProvider(default_model="github-copilot/gpt-5.3-codex")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(content="Let me do that", tool_calls=None),
            ),
            SimpleNamespace(
                finish_reason="tool_calls",
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[_tool_call("read_file", '{"path": "README.md"}')],
                ),
            ),
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )

    parsed = provider._parse_response(response)

    assert parsed.content == "Let me do that"
    assert parsed.finish_reason == "tool_calls"
    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].name == "read_file"
    assert parsed.tool_calls[0].arguments == {"path": "README.md"}


def test_parse_response_handles_single_choice_without_tool_calls() -> None:
    provider = LiteLLMProvider(default_model="openrouter/minimax/minimax-m2.5")
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop", message=SimpleNamespace(content="Done", tool_calls=None)
            )
        ],
        usage=None,
    )

    parsed = provider._parse_response(response)

    assert parsed.content == "Done"
    assert parsed.finish_reason == "stop"
    assert parsed.tool_calls == []


def test_sanitize_messages_truncates_long_tool_call_id() -> None:
    long_id = "x" * 80

    sanitized = LiteLLMProvider._sanitize_messages(
        [{"role": "tool", "tool_call_id": long_id, "content": "ok", "name": "exec"}]
    )

    assert len(sanitized[0]["tool_call_id"]) == 64
    assert sanitized[0]["tool_call_id"] == long_id[:32] + long_id[-32:]


@pytest.mark.asyncio
async def test_stream_chat_emits_incremental_text_deltas() -> None:
    provider = LiteLLMProvider(default_model="openrouter/minimax/minimax-m2.5")
    seen: list[str] = []

    class _FakeStream:
        def __init__(self, chunks):
            self._chunks = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="hel", tool_calls=None), finish_reason=None)],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="lo", tool_calls=None), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        ),
    ]

    async def _on_text_delta(delta: str) -> None:
        seen.append(delta)

    with patch("nanobot.providers.litellm_provider.acompletion", return_value=_FakeStream(chunks)):
        response = await provider.stream_chat(
            messages=[{"role": "user", "content": "hello"}],
            on_text_delta=_on_text_delta,
        )

    assert response.content == "hello"
    assert response.streamed_content is True
    assert response.finish_reason == "stop"
    assert seen == ["hel", "lo"]


@pytest.mark.asyncio
async def test_stream_chat_preserves_reasoning_separately_from_text() -> None:
    provider = LiteLLMProvider(default_model="openrouter/minimax/minimax-m2.5")
    seen_text: list[str] = []
    seen_reasoning: list[str] = []

    class _FakeStream:
        def __init__(self, chunks):
            self._chunks = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    chunks = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=None, reasoning_content="thinking ", tool_calls=None),
                    finish_reason=None,
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="answer", tool_calls=None), finish_reason="stop")],
            usage=None,
        ),
    ]

    async def _on_text_delta(delta: str) -> None:
        seen_text.append(delta)

    async def _on_reasoning_delta(delta: str) -> None:
        seen_reasoning.append(delta)

    with patch("nanobot.providers.litellm_provider.acompletion", return_value=_FakeStream(chunks)):
        response = await provider.stream_chat(
            messages=[{"role": "user", "content": "hello"}],
            on_text_delta=_on_text_delta,
            on_reasoning_delta=_on_reasoning_delta,
        )

    assert response.content == "answer"
    assert response.reasoning_content == "thinking "
    assert seen_text == ["answer"]
    assert seen_reasoning == ["thinking "]


@pytest.mark.asyncio
async def test_stream_chat_falls_back_when_stream_has_no_content() -> None:
    provider = LiteLLMProvider(default_model="openrouter/minimax/minimax-m2.5")

    class _FakeStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    fallback_response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="Done", tool_calls=None))],
        usage=None,
    )

    async def _on_text_delta(_delta: str) -> None:
        return None

    with patch(
        "nanobot.providers.litellm_provider.acompletion",
        side_effect=[_FakeStream(), fallback_response],
    ):
        response = await provider.stream_chat(
            messages=[{"role": "user", "content": "hello"}],
            on_text_delta=_on_text_delta,
        )

    assert response.content == "Done"
    assert response.streamed_content is False
