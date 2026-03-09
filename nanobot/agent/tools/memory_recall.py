"""Subconscious memory recall tool."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.agent.subconscious import SubconsciousService


class MemoryRecallTool(Tool):
    """Search long-term memory for facts extracted from past conversations."""

    name = "memory_search"
    description = (
        "Search your subconscious memory for relevant entities, preferences, decisions, "
        "and past events. Use when the user references prior interactions, people, projects, "
        "or when background context would improve your response."
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

    def __init__(self, service: SubconsciousService):
        self._service = service

    async def execute(self, query: str, **kwargs: Any) -> str:
        result = await self._service.search(query)
        return result or "No relevant memories found."
