"""Tool registry for dynamic tool management."""

from typing import Any

from nanobot.agent.tools.base import Tool


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    async def execute(self, name: Any, params: dict[str, Any]) -> str:
        """Execute a tool by name with given parameters."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"
        _RETRY_VALID_TOOL_HINT = (
            "\n\n[Your previous tool call was malformed or used an unknown tool. "
            "Retry with a valid tool call using one of the available tool names exactly as defined. "
            "Do not leave function.name empty or null.]"
        )

        normalized_name = name.strip() if isinstance(name, str) else ""
        tool = self._tools.get(normalized_name)
        if not tool:
            available = ', '.join(self.tool_names)
            if not normalized_name:
                return (
                    "Error: Invalid tool call: function.name was empty or null. "
                    f"Available tools: {available}."
                    + _RETRY_VALID_TOOL_HINT
                )
            return (
                f"Error: Tool '{normalized_name}' not found. Available: {available}."
                + _RETRY_VALID_TOOL_HINT
            )

        try:
            errors = tool.validate_params(params)
            if errors:
                return (
                    f"Error: Invalid parameters for tool '{normalized_name}': "
                    + "; ".join(errors)
                    + _HINT
                )
            result = await tool.execute(**params)

            # Redact secrets from all tool output
            if isinstance(result, str):
                from nanobot.security.redact import redact_secrets
                result = redact_secrets(result)

            if isinstance(result, str) and result.startswith("Error"):
                return result + _HINT
            return result
        except Exception as e:
            return f"Error executing {normalized_name or name}: {str(e)}" + _HINT

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
