"""Remote execution tool: dispatch shell commands to connected nanobot nodes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nanobot.agent.tools.base import Tool

if TYPE_CHECKING:
    from nanobot.nodes.registry import NodeRegistry


class RemoteExecTool(Tool):
    """Execute shell commands on remote nanobot nodes."""

    # Reuse ExecTool deny patterns for gateway-side safety check
    _DENY_PATTERNS = [
        r"\brm\s+-[rf]{1,2}\b",
        r"\bdel\s+/[fq]\b",
        r"\brmdir\s+/s\b",
        r"(?:^|[;&|]\s*)format\b",
        r"\b(mkfs|diskpart)\b",
        r"\bdd\s+if=",
        r">\s*/dev/sd",
        r"\b(shutdown|reboot|poweroff)\b",
        r":\(\)\s*\{.*\};\s*:",
    ]

    def __init__(self, registry: NodeRegistry):
        self._registry = registry

    @property
    def name(self) -> str:
        return "remote_exec"

    @property
    def description(self) -> str:
        nodes = self._registry.connected_node_ids
        if nodes:
            node_list = ", ".join(nodes)
            return (
                f"Execute a shell command on a remote nanobot node. "
                f"Connected nodes: {node_list}"
            )
        return "Execute a shell command on a remote nanobot node. No nodes currently connected."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "node": {
                    "type": "string",
                    "description": "Node ID to execute the command on",
                },
                "command": {
                    "type": "string",
                    "description": "The shell command to execute on the remote node",
                },
                "description": {
                    "type": "string",
                    "description": "Brief human-readable summary of what this command does",
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command on the node",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Command timeout in seconds (default 60)",
                },
            },
            "required": ["node", "command", "description"],
        }

    async def execute(
        self,
        node: str,
        command: str,
        description: str = "",
        working_dir: str | None = None,
        timeout: int = 60,
        **kwargs: Any,
    ) -> str:
        import re

        # Gateway-side safety check
        lower = command.strip().lower()
        for pattern in self._DENY_PATTERNS:
            if re.search(pattern, lower):
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        result = await self._registry.send_command(
            node_id=node,
            command=command,
            working_dir=working_dir,
            timeout=timeout,
        )

        if "error" in result:
            return f"Error: {result['error']}"

        parts = []
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code", 0)

        if stdout:
            parts.append(stdout)
        if stderr:
            parts.append(f"STDERR:\n{stderr}")
        if exit_code != 0:
            parts.append(f"\nExit code: {exit_code}")

        output = "\n".join(parts) if parts else "(no output)"

        # Truncate long output
        max_len = 10000
        if len(output) > max_len:
            output = output[:max_len] + f"\n... (truncated, {len(output) - max_len} more chars)"

        return output
