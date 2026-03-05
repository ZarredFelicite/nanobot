"""Tests for memU bridge service and tool."""

import asyncio
import importlib
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from nanobot.config.schema import MemUConfig

# Prevent the heavy nanobot.agent.__init__ import chain (httpx, litellm, etc.)
# by pre-seeding the package as an empty namespace before importing submodules.
if "nanobot.agent" not in sys.modules:
    _pkg = types.ModuleType("nanobot.agent")
    _pkg.__path__ = [str(__import__("pathlib").Path(__file__).resolve().parent.parent / "nanobot" / "agent")]
    _pkg.__package__ = "nanobot.agent"
    sys.modules["nanobot.agent"] = _pkg

if "nanobot.agent.tools" not in sys.modules:
    _tools_pkg = types.ModuleType("nanobot.agent.tools")
    _tools_pkg.__path__ = [str(__import__("pathlib").Path(__file__).resolve().parent.parent / "nanobot" / "agent" / "tools")]
    _tools_pkg.__package__ = "nanobot.agent.tools"
    sys.modules["nanobot.agent.tools"] = _tools_pkg

from nanobot.agent.memu_service import MemUBridge  # noqa: E402
from nanobot.agent.tools.memu_retrieve import MemURetrieveTool  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bridge(enabled=True, **overrides):
    """Create a MemUBridge with a mock config."""
    cfg = MemUConfig(enabled=enabled, **overrides)
    return MemUBridge(cfg)


def _mock_service():
    """Return a mock MemUService."""
    svc = MagicMock()
    svc.memorize = MagicMock()
    svc.retrieve = MagicMock(return_value=[])
    svc.close = MagicMock()
    return svc


# ---------------------------------------------------------------------------
# Buffer filtering
# ---------------------------------------------------------------------------

class TestBufferFiltering:
    """feed_messages should only keep user/assistant text messages."""

    def test_filters_tool_messages(self):
        bridge = _make_bridge()
        bridge._available = True
        bridge._service = _mock_service()

        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "tool", "content": "tool result"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "system", "content": "system prompt"},
        ]
        bridge.feed_messages(messages)

        assert len(bridge._buffer) == 2
        assert bridge._buffer[0]["role"] == "user"
        assert bridge._buffer[1]["role"] == "assistant"

    def test_skips_empty_content(self):
        bridge = _make_bridge()
        bridge._available = True
        bridge._service = _mock_service()

        messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "   "},
            {"role": "user", "content": "valid"},
        ]
        bridge.feed_messages(messages)

        assert len(bridge._buffer) == 1
        assert bridge._buffer[0]["content"] == "valid"

    def test_skips_non_string_content(self):
        bridge = _make_bridge()
        bridge._available = True
        bridge._service = _mock_service()

        messages = [
            {"role": "user", "content": [{"type": "image_url"}]},
            {"role": "user", "content": "text message"},
        ]
        bridge.feed_messages(messages)

        assert len(bridge._buffer) == 1


# ---------------------------------------------------------------------------
# No-op when disabled / unavailable
# ---------------------------------------------------------------------------

class TestNoOp:
    """All operations should be no-ops when service is unavailable."""

    def test_feed_noop_when_unavailable(self):
        bridge = _make_bridge()
        # _available is False by default (no initialize called)
        bridge.feed_messages([{"role": "user", "content": "hello"}])
        assert len(bridge._buffer) == 0

    @pytest.mark.asyncio
    async def test_retrieve_noop_when_unavailable(self):
        bridge = _make_bridge()
        result = await bridge.retrieve("test query")
        assert "not available" in result

    @pytest.mark.asyncio
    async def test_close_noop_when_unavailable(self):
        bridge = _make_bridge()
        await bridge.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_initialize_noop_when_disabled(self):
        bridge = _make_bridge(enabled=False)
        await bridge.initialize()
        assert bridge._available is False


# ---------------------------------------------------------------------------
# Retrieval formatting
# ---------------------------------------------------------------------------

class TestRetrievalFormatting:
    @pytest.mark.asyncio
    async def test_formats_results(self):
        bridge = _make_bridge()
        bridge._available = True
        bridge._service = _mock_service()
        bridge._service.retrieve.return_value = [
            {"fact": "User prefers dark mode", "score": 0.95},
            {"fact": "User's name is Alice", "score": 0.87},
        ]

        result = await bridge.retrieve("user preferences")

        assert "2 relevant" in result
        assert "User prefers dark mode" in result
        assert "0.95" in result
        assert "User's name is Alice" in result

    @pytest.mark.asyncio
    async def test_no_results(self):
        bridge = _make_bridge()
        bridge._available = True
        bridge._service = _mock_service()
        bridge._service.retrieve.return_value = []

        result = await bridge.retrieve("nonexistent")
        assert "No relevant memories" in result

    @pytest.mark.asyncio
    async def test_handles_text_key_fallback(self):
        bridge = _make_bridge()
        bridge._available = True
        bridge._service = _mock_service()
        bridge._service.retrieve.return_value = [
            {"text": "Some memory text"},
        ]

        result = await bridge.retrieve("query")
        assert "Some memory text" in result


# ---------------------------------------------------------------------------
# Tool delegation
# ---------------------------------------------------------------------------

class TestMemURetrieveTool:
    @pytest.mark.asyncio
    async def test_tool_delegates_to_bridge(self):
        bridge = _make_bridge()
        bridge._available = True
        bridge._service = _mock_service()
        bridge._service.retrieve.return_value = [
            {"fact": "test fact", "score": 0.9},
        ]

        tool = MemURetrieveTool(bridge)

        assert tool.name == "memory_search"
        result = await tool.execute(query="test")
        assert "test fact" in result

    def test_tool_schema(self):
        bridge = _make_bridge()
        tool = MemURetrieveTool(bridge)
        schema = tool.to_schema()

        assert schema["type"] == "function"
        assert schema["function"]["name"] == "memory_search"
        assert "query" in schema["function"]["parameters"]["properties"]
        assert "query" in schema["function"]["parameters"]["required"]


# ---------------------------------------------------------------------------
# Flush behavior
# ---------------------------------------------------------------------------

class TestFlush:
    @pytest.mark.asyncio
    async def test_flush_sends_to_service(self):
        bridge = _make_bridge()
        bridge._available = True
        bridge._service = _mock_service()

        bridge._buffer = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]

        await bridge._flush()

        bridge._service.memorize.assert_called_once()
        call_text = bridge._service.memorize.call_args[0][0]
        assert "[user]: Hello" in call_text
        assert "[assistant]: Hi" in call_text
        assert len(bridge._buffer) == 0

    @pytest.mark.asyncio
    async def test_flush_noop_when_empty(self):
        bridge = _make_bridge()
        bridge._available = True
        bridge._service = _mock_service()

        await bridge._flush()
        bridge._service.memorize.assert_not_called()
