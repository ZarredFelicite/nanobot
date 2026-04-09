from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.session.manager import Session, SessionManager


def _mk_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    loop._TOOL_RESULT_MAX_CHARS = 500
    loop._subconscious = None
    return loop


def test_save_turn_skips_multimodal_user_when_only_runtime_context() -> None:
    loop = _mk_loop()
    session = Session(key="test:runtime-only")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{"role": "user", "content": [{"type": "text", "text": runtime}]}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_image_placeholder_after_runtime_strip() -> None:
    loop = _mk_loop()
    session = Session(key="test:image")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": runtime},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image]"}]


def test_save_turn_persists_usage_on_final_assistant_message() -> None:
    loop = _mk_loop()
    session = Session(key="test:usage")

    loop._save_turn(
        session,
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read"}}],
            },
            {
                "role": "assistant",
                "content": "Done.",
            },
        ],
        skip=0,
        usage={"prompt_tokens": 1000, "completion_tokens": 42},
        model="openai-codex/gpt-5.3-codex",
    )

    assert session.messages[-1].get("usage", {}).get("completion_tokens") == 42
    assert session.messages[-1].get("model") == "openai-codex/gpt-5.3-codex"


@pytest.mark.asyncio
async def test_process_system_direct_persists_into_target_session(tmp_path: Path) -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop._TOOL_RESULT_MAX_CHARS = 500
    loop._subconscious = None
    loop.sessions = SessionManager(tmp_path)
    loop.context = ContextBuilder(tmp_path)
    loop.model = "openai-codex/gpt-5.4"
    loop.memory_window = 20
    loop._last_llm_usage = {}
    loop._connect_mcp = AsyncMock()
    loop._refresh_model_limits = AsyncMock()
    loop._set_tool_context = lambda *args, **kwargs: None

    async def fake_run_agent_loop(messages, model=None):
        return (
            "Reminder delivered.",
            None,
            [*messages, {"role": "assistant", "content": "Reminder delivered."}],
            {"prompt_tokens": 10, "completion_tokens": 3},
        )

    loop._run_agent_loop = fake_run_agent_loop

    response = await loop.process_system_direct(
        "[Scheduled Task] Timer finished.",
        session_key="main",
        channel="telegram",
        chat_id="owner",
    )

    assert response == "Reminder delivered."
    session = loop.sessions.get_or_create("main")
    assert [m["role"] for m in session.messages] == ["system", "assistant"]
    assert session.messages[0]["content"] == "[Scheduled Task] Timer finished."
    assert session.messages[1]["content"] == "Reminder delivered."
    assert loop._last_llm_usage["main"]["completion_tokens"] == 3
