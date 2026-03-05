"""memU memory retrieval tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.agent.memu_service import MemUBridge


class MemURetrieveTool(Tool):
    """Search long-term memory for facts extracted from past conversations."""

    name = "memory_search"
    description = (
        "Search your long-term memory for relevant facts, preferences, and context "
        "from past conversations. Use when the user references prior interactions or "
        "when background context would improve your response."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language query describing what to recall",
            }
        },
        "required": ["query"],
    }

    def __init__(self, bridge: MemUBridge):
        self._bridge = bridge

    async def execute(self, query: str, **kwargs: Any) -> str:
        return await self._bridge.retrieve(query)
