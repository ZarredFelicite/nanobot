from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, LLMResponse


def _make_loop(provider: MagicMock) -> AgentLoop:
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())
    with (
        patch("nanobot.agent.loop.ContextBuilder"),
        patch("nanobot.agent.loop.SessionManager"),
        patch("nanobot.agent.loop.SubagentManager"),
    ):
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=workspace)
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


@pytest.mark.asyncio
async def test_chat_with_retry_retries_transient_error_then_succeeds() -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat = AsyncMock(
        side_effect=[
            LLMResponse(content="Error calling Codex:", finish_reason="error"),
            LLMResponse(content="Done", finish_reason="stop"),
        ]
    )
    loop = _make_loop(provider)

    with patch("nanobot.agent.loop.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        response = await loop._chat_with_retry(
            provider,
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            model="test-model",
            temperature=0.1,
            max_tokens=128,
            reasoning_effort=None,
        )

    assert response.content == "Done"
    assert provider.chat.await_count == 2
    sleep_mock.assert_awaited_once_with(1.0)


@pytest.mark.asyncio
async def test_chat_with_retry_does_not_retry_non_transient_error() -> None:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat = AsyncMock(
        return_value=LLMResponse(
            content="Error calling LLM: 401 Unauthorized - missing API key",
            finish_reason="error",
        )
    )
    loop = _make_loop(provider)

    with patch("nanobot.agent.loop.asyncio.sleep", new=AsyncMock()) as sleep_mock:
        response = await loop._chat_with_retry(
            provider,
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            model="test-model",
            temperature=0.1,
            max_tokens=128,
            reasoning_effort=None,
        )

    assert response.is_error is True
    assert provider.chat.await_count == 1
    sleep_mock.assert_not_awaited()


class _StreamingProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key=None, api_base=None)
        self.chat_called = False

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
    ) -> LLMResponse:
        self.chat_called = True
        return LLMResponse(content="fallback", finish_reason="stop")

    async def stream_chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        on_text_delta=None,
        on_reasoning_delta=None,
    ) -> LLMResponse:
        if on_text_delta:
            await on_text_delta("hel")
            await on_text_delta("lo")
        return LLMResponse(content="hello", finish_reason="stop", streamed_content=True)

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_chat_with_retry_uses_provider_stream_chat_for_incremental_deltas() -> None:
    provider = _StreamingProvider()
    loop = _make_loop(MagicMock())
    seen: list[str] = []

    async def _on_text_delta(delta: str) -> None:
        seen.append(delta)

    response = await loop._chat_with_retry(
        provider,
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        model="test-model",
        temperature=0.1,
        max_tokens=128,
        reasoning_effort=None,
        on_text_delta=_on_text_delta,
    )

    assert response.content == "hello"
    assert response.streamed_content is True
    assert seen == ["hel", "lo"]
    assert provider.chat_called is False
