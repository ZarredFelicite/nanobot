"""Tests for parallel tool execution."""

import asyncio
import time

import pytest

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.filesystem import ReadFileTool, ListDirTool
from nanobot.agent.tools.web import WebSearchTool, WebFetchTool
from nanobot.agent.tools.memory_recall import MemoryRecallTool


class SlowReadTool(Tool):
    """A mock tool that simulates a slow read operation."""

    name = "slow_read"
    description = "Slow read for testing"
    parameters = {"type": "object", "properties": {}, "required": []}

    @property
    def parallel_safe(self) -> bool:
        return True

    async def execute(self, **kwargs) -> str:
        await asyncio.sleep(0.1)
        return "read_result"


class SlowUnsafeTool(Tool):
    """A mock tool that is NOT parallel-safe."""

    name = "slow_unsafe"
    description = "Slow unsafe for testing"
    parameters = {"type": "object", "properties": {}, "required": []}

    # parallel_safe defaults to False via base class

    async def execute(self, **kwargs) -> str:
        await asyncio.sleep(0.1)
        return "unsafe_result"


def test_base_tool_parallel_safe_default():
    """Base Tool.parallel_safe defaults to False."""
    tool = SlowUnsafeTool()
    assert tool.parallel_safe is False


def test_read_file_parallel_safe():
    assert ReadFileTool().parallel_safe is True


def test_list_dir_parallel_safe():
    assert ListDirTool().parallel_safe is True


def test_web_search_parallel_safe():
    assert WebSearchTool().parallel_safe is True


def test_web_fetch_parallel_safe():
    assert WebFetchTool().parallel_safe is True


def test_override_parallel_safe():
    """Tools that override parallel_safe to True are correctly marked."""
    tool = SlowReadTool()
    assert tool.parallel_safe is True


@pytest.mark.asyncio
async def test_parallel_execution_faster_than_sequential():
    """Parallel-safe tools should execute concurrently."""
    tools = [SlowReadTool() for _ in range(5)]

    # Sequential timing
    seq_start = time.monotonic()
    for tool in tools:
        await tool.execute()
    seq_elapsed = time.monotonic() - seq_start

    # Parallel timing
    par_start = time.monotonic()
    await asyncio.gather(*(tool.execute() for tool in tools))
    par_elapsed = time.monotonic() - par_start

    # Parallel should be significantly faster (at least 2x)
    assert par_elapsed < seq_elapsed * 0.7


@pytest.mark.asyncio
async def test_parallel_results_preserve_order():
    """asyncio.gather preserves result order matching input order."""

    class OrderedTool(Tool):
        name = "ordered"
        description = "test"
        parameters = {"type": "object", "properties": {}, "required": []}

        def __init__(self, idx: int, delay: float):
            self.idx = idx
            self.delay = delay

        @property
        def parallel_safe(self) -> bool:
            return True

        async def execute(self, **kwargs) -> str:
            await asyncio.sleep(self.delay)
            return f"result_{self.idx}"

    # Tool 0 is slowest, tool 2 is fastest — results should still be in order
    tools = [OrderedTool(0, 0.15), OrderedTool(1, 0.1), OrderedTool(2, 0.05)]
    results = await asyncio.gather(*(t.execute() for t in tools))
    assert results == ["result_0", "result_1", "result_2"]
