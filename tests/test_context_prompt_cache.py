"""Tests for cache-friendly prompt construction."""

from __future__ import annotations

from datetime import datetime as real_datetime
from pathlib import Path
import datetime as datetime_module

from nanobot.agent.context import ContextBuilder


class _FakeDatetime(real_datetime):
    current = real_datetime(2026, 2, 24, 13, 59)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls.current


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    return workspace


def test_system_prompt_stays_stable_when_clock_changes(tmp_path, monkeypatch) -> None:
    """System prompt should not change just because wall clock minute changes."""
    monkeypatch.setattr(datetime_module, "datetime", _FakeDatetime)

    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    _FakeDatetime.current = real_datetime(2026, 2, 24, 13, 59)
    prompt1 = builder.build_system_prompt()

    _FakeDatetime.current = real_datetime(2026, 2, 24, 14, 0)
    prompt2 = builder.build_system_prompt()

    assert prompt1 == prompt2


def test_runtime_context_is_separate_untrusted_user_message(tmp_path) -> None:
    """Runtime metadata should be merged with the user message."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Return exactly: OK",
        channel="cli",
        chat_id="direct",
    )

    assert messages[0]["role"] == "system"
    assert "## Current Session" not in messages[0]["content"]

    # Runtime context is now merged with user message into a single message
    assert messages[-1]["role"] == "user"
    user_content = messages[-1]["content"]
    assert isinstance(user_content, str)
    assert ContextBuilder._RUNTIME_CONTEXT_TAG in user_content
    assert "Current Time:" in user_content
    assert "Channel: cli" in user_content
    assert "Chat ID: direct" in user_content
    assert "Return exactly: OK" in user_content


def test_identity_uses_identity_md_when_present(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    (workspace / "IDENTITY.md").write_text(
        "- I am Zarred's focused coding copilot.", encoding="utf-8"
    )
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    assert "You are based on nanobot - an AI assistant, here is your identity:" in prompt
    assert "- I am Zarred's focused coding copilot." in prompt
    assert "## IDENTITY.md" not in prompt


def test_identity_falls_back_when_identity_md_missing(tmp_path) -> None:
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    prompt = builder.build_system_prompt()

    assert "You are nanobot, a helpful AI assistant." in prompt


def test_memories_appended_to_user_message_not_system_prompt(tmp_path) -> None:
    """Relevant memories should be appended to the user message, not the system prompt."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)
    memories = "User prefers dark mode.\n[[Zarred]] works on nanobot."

    messages = builder.build_messages(
        history=[],
        current_message="Hello",
        relevant_memories=memories,
    )

    # System prompt must NOT contain memories
    assert memories not in messages[0]["content"]

    # Should have just: system, user
    assert len(messages) == 2
    # Memories appended to user message with context tag
    user_content = messages[1]["content"]
    assert memories in user_content
    assert ContextBuilder._MEMORY_CONTEXT_TAG in user_content


def test_no_memories_skips_injection(tmp_path) -> None:
    """When no memories, user message should not contain memory tag."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    messages = builder.build_messages(
        history=[],
        current_message="Hello",
        relevant_memories=None,
    )

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert ContextBuilder._MEMORY_CONTEXT_TAG not in messages[1]["content"]


def test_system_prompt_stable_with_different_memories(tmp_path) -> None:
    """System prompt should be identical regardless of what memories are injected."""
    workspace = _make_workspace(tmp_path)
    builder = ContextBuilder(workspace)

    msgs1 = builder.build_messages(
        history=[], current_message="Hi", relevant_memories="memory A",
    )
    msgs2 = builder.build_messages(
        history=[], current_message="Hi", relevant_memories="memory B",
    )
    msgs3 = builder.build_messages(
        history=[], current_message="Hi", relevant_memories=None,
    )

    assert msgs1[0]["content"] == msgs2[0]["content"] == msgs3[0]["content"]
