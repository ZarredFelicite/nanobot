"""Subagent tool — delegates complex tasks to Pi via RPC subprocess."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool

# Files injected into Pi's system prompt for environment context.
_CONTEXT_FILES = ["TOOLS.md", "USER.md", "PI-AGENTS.md"]


class SubagentTool(Tool):
    """Delegate complex, multi-step tasks to a Pi subagent.

    Pi runs as an RPC subprocess with its own session, tools
    (read/write/edit/bash/grep/find), and context window.  Results stream
    back as structured JSONL events.
    """

    def __init__(
        self,
        workspace: Path,
        session_dir: Path | None = None,
        model: str | None = None,
        timeout: int = 600,
    ):
        self._workspace = workspace
        self._session_dir = session_dir or workspace / "sessions" / "pi"
        self._model = model
        self._timeout = timeout
        self._session_dir.mkdir(parents=True, exist_ok=True)

    @property
    def name(self) -> str:
        return "subagent"

    @property
    def description(self) -> str:
        return (
            "Delegate a complex task to a Pi subagent. "
            "The subagent has its own tools (read, write, edit, bash, grep, find) "
            "and maintains persistent sessions for multi-step work. Use this for "
            "tasks that require deep code exploration, refactoring, debugging, "
            "implementation across multiple files, research, or any work that "
            "benefits from an autonomous agent with its own context window. "
            "The task description should be detailed enough for the subagent "
            "to work autonomously."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Detailed description of the task to perform",
                },
                "session": {
                    "type": "string",
                    "description": (
                        "Session name for continuity across related tasks. "
                        "Use the same name to continue previous work."
                    ),
                },
                "working_dir": {
                    "type": "string",
                    "description": "Working directory for the task (defaults to workspace)",
                },
                "context": {
                    "type": "string",
                    "description": "Optional extra context to append to the subagent's system prompt",
                },
            },
            "required": ["task", "session"],
        }

    def _build_system_prompt(self, extra_context: str | None = None) -> str:
        """Build system prompt from workspace context files."""
        parts: list[str] = []
        for filename in _CONTEXT_FILES:
            filepath = self._workspace / filename
            if filepath.exists():
                try:
                    content = filepath.read_text(encoding="utf-8").strip()
                    if content:
                        parts.append(f"# {filename}\n\n{content}")
                except Exception:
                    pass
        if extra_context:
            parts.append(extra_context)
        return "\n\n---\n\n".join(parts)

    async def execute(
        self,
        task: str,
        session: str = "default",
        working_dir: str | None = None,
        context: str | None = None,
        **kwargs: Any,
    ) -> str:
        if not shutil.which("pi"):
            return "Error: pi (pi-coding-agent) is not installed or not in PATH"

        cwd = working_dir or str(self._workspace)
        session_file = self._session_dir / f"{session}.jsonl"
        system_prompt = self._build_system_prompt(context)

        cmd = ["pi", "--mode", "rpc", "--session", str(session_file)]

        if self._model:
            cmd.extend(["--model", self._model])

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except Exception as e:
            return f"Error: Failed to start subagent: {e}"

        try:
            return await asyncio.wait_for(
                self._run_rpc(proc, task),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            return f"Error: Subagent timed out after {self._timeout}s"
        except Exception as e:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            return f"Error: Subagent failed: {e}"

    async def _run_rpc(self, proc: asyncio.subprocess.Process, task: str) -> str:
        """Send prompt and collect events until agent_end."""
        assert proc.stdin is not None
        assert proc.stdout is not None

        prompt_cmd = json.dumps({"type": "prompt", "message": task}) + "\n"
        proc.stdin.write(prompt_cmd.encode())
        await proc.stdin.drain()

        text_parts: list[str] = []
        tool_results: list[str] = []
        error: str | None = None

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "message_update":
                ame = event.get("assistantMessageEvent", {})
                ame_type = ame.get("type", "")
                if ame_type == "text_delta":
                    delta = ame.get("delta", "")
                    if delta:
                        text_parts.append(delta)
                elif ame_type == "error":
                    error = ame.get("reason", "unknown error")

            elif etype == "tool_execution_end":
                tool_name = event.get("toolName", "?")
                is_error = event.get("isError", False)
                result = event.get("result", {})
                result_text = ""
                if isinstance(result, dict):
                    for part in result.get("content", []):
                        if isinstance(part, dict) and part.get("type") == "text":
                            result_text = part.get("text", "")
                            break
                status = "ERROR" if is_error else "OK"
                summary = result_text[:200] if result_text else ""
                tool_results.append(f"[{tool_name}] {status}: {summary}")

            elif etype == "agent_end":
                break

            elif etype == "response":
                if not event.get("success", True):
                    error = event.get("error", "unknown RPC error")
                    break

        # Shut down gracefully
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            proc.kill()

        if error:
            return f"Error: {error}"

        output_parts: list[str] = []
        full_text = "".join(text_parts).strip()
        if full_text:
            output_parts.append(full_text)
        if tool_results:
            output_parts.append("\n--- Tool Executions ---")
            output_parts.extend(tool_results)

        result = "\n".join(output_parts) if output_parts else "(no output from subagent)"

        max_len = 15000
        if len(result) > max_len:
            result = result[:max_len] + f"\n... (truncated, {len(result) - max_len} more chars)"

        return result
