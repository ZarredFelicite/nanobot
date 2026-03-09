"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import weakref
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig, SubconsciousConfig
    from nanobot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    _TOOL_RESULT_MAX_CHARS = 500

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        context_tokens: int = 200000,
        reserve_tokens_floor: int = 20000,
        reasoning_effort: str | None = None,
        brave_api_key: str | None = None,
        web_proxy: str | None = None,
        exec_config: ExecToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        channels_config: ChannelsConfig | None = None,
        subconscious_config: SubconsciousConfig | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig

        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.context_tokens = max(4096, context_tokens)
        self.reserve_tokens_floor = max(0, reserve_tokens_floor)
        self.reasoning_effort = reasoning_effort
        self.brave_api_key = brave_api_key
        self.web_proxy = web_proxy
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry()
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=reasoning_effort,
            brave_api_key=brave_api_key,
            web_proxy=web_proxy,
            exec_config=self.exec_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        # Subconscious memory service (lazy init)
        self._subconscious = None
        self._subconscious_config = subconscious_config
        if subconscious_config and subconscious_config.enabled:
            from nanobot.agent.subconscious import SubconsciousService

            self._subconscious = SubconsciousService(workspace, subconscious_config)

        self._running = False
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: set[asyncio.Task] = set()  # Strong refs to in-flight tasks
        self._consolidation_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )
        self._active_tasks: dict[str, list[asyncio.Task]] = {}  # session_key -> tasks
        self._processing_lock = asyncio.Lock()
        self._codex_provider: LLMProvider | None = None
        self._last_context_stats: dict[str, dict[str, Any]] = {}
        self._last_llm_usage: dict[str, dict[str, Any]] = {}
        # Permission callback: async (tool_name, tool_call_id, args) -> "once"|"always"|"reject"
        self._permission_callback: Callable[..., Awaitable[str]] | None = None
        self._require_approval: list[str] = []  # Tool names that need user approval
        self._session_auto_approve: dict[str, set[str]] = {}  # session_key -> auto-approved tools
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir))
        self.tools.register(
            ExecTool(
                working_dir=str(self.workspace),
                timeout=self.exec_config.timeout,
                restrict_to_workspace=self.restrict_to_workspace,
                path_append=self.exec_config.path_append,
            )
        )
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))
        if self._subconscious:
            from nanobot.agent.tools.memory_recall import MemoryRecallTool

            self.tools.register(MemoryRecallTool(self._subconscious))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers

        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message): {}", e)
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        for name in ("message", "spawn", "cron"):
            if tool := self.tools.get(name):
                set_context = getattr(tool, "set_context", None)
                if callable(set_context):
                    set_context(channel, chat_id, *([message_id] if name == "message" else []))

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""

        def _fmt(tc):
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            val = next(iter(args.values()), None) if isinstance(args, dict) else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'

        return ", ".join(_fmt(tc) for tc in tool_calls)

    @staticmethod
    def _fallback_token_count(messages: list[dict[str, Any]]) -> int:
        """Fallback token estimator when model tokenizer is unavailable."""
        chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        chars += len(str(block.get("text", "")))
                    elif block.get("type") == "image_url":
                        chars += 256
                    else:
                        chars += len(str(block))
            else:
                chars += len(str(content))
            chars += 24
        return max(1, chars // 4)

    def _count_tokens(self, messages: list[dict[str, Any]], model: str) -> int:
        """Count prompt tokens with model-aware tokenization."""
        if not messages:
            return 0

        try:
            from litellm import token_counter

            resolved_model = model
            resolve_model = getattr(self.provider, "_resolve_model", None)
            if callable(resolve_model):
                candidate = resolve_model(model)
                if isinstance(candidate, str) and candidate:
                    resolved_model = candidate
            model_name = resolved_model if isinstance(resolved_model, str) else model
            return int(token_counter(model=model_name, messages=messages) or 0)
        except Exception:
            return self._fallback_token_count(messages)

    def _context_budget(self) -> int:
        """Maximum prompt tokens before compaction is required."""
        return max(512, self.context_tokens - self.max_tokens - self.reserve_tokens_floor)

    def _context_usage_breakdown(
        self, messages: list[dict[str, Any]], model: str
    ) -> dict[str, int]:
        """Compute token breakdown for system/history/current message."""
        total = self._count_tokens(messages, model)
        if not messages:
            return {"system": 0, "history": 0, "current": 0, "total": total}

        system = self._count_tokens(messages[:1], model)
        without_current = self._count_tokens(messages[:-1], model) if len(messages) > 1 else system
        history = max(0, without_current - system)
        current = max(0, total - without_current)
        return {"system": system, "history": history, "current": current, "total": total}

    def _trim_history_by_budget(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        media: list[str] | None,
        channel: str,
        chat_id: str,
        model: str,
        relevant_memories: str | None = None,
    ) -> list[dict[str, Any]]:
        """Trim oldest turns until prompt fits the token budget."""
        trimmed = list(history)
        budget = self._context_budget()

        while trimmed:
            candidate = self.context.build_messages(
                history=trimmed,
                current_message=current_message,
                media=media if media else None,
                channel=channel,
                chat_id=chat_id,
                relevant_memories=relevant_memories,
            )
            if self._count_tokens(candidate, model) <= budget:
                break

            trimmed = trimmed[1:]
            while trimmed and trimmed[0].get("role") != "user":
                trimmed = trimmed[1:]

        return trimmed

    def get_last_context_stats(self, session_key: str) -> dict[str, Any] | None:
        """Get the most recent context usage stats for a session."""
        return self._last_context_stats.get(session_key)

    def get_last_llm_usage(self, session_key: str) -> dict[str, Any] | None:
        """Get the most recent model usage payload for a session."""
        return self._last_llm_usage.get(session_key)

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        """Best-effort integer conversion for persisted metadata values."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _record_context_usage(self, session: Session, stats: dict[str, Any]) -> None:
        """Persist per-session context usage totals and latest breakdown."""
        final = stats.get("final") if isinstance(stats, dict) else None
        if not isinstance(final, dict):
            return

        usage = session.metadata.get("context_usage")
        if not isinstance(usage, dict):
            usage = {}

        totals = usage.get("totals")
        if not isinstance(totals, dict):
            totals = {
                "requests": 0,
                "system_tokens": 0,
                "history_tokens": 0,
                "current_tokens": 0,
                "total_tokens": 0,
                "compaction_passes": 0,
                "trimmed_history_messages": 0,
            }

        totals["requests"] = self._to_int(totals.get("requests")) + 1
        totals["system_tokens"] = self._to_int(totals.get("system_tokens")) + self._to_int(
            final.get("system")
        )
        totals["history_tokens"] = self._to_int(totals.get("history_tokens")) + self._to_int(
            final.get("history")
        )
        totals["current_tokens"] = self._to_int(totals.get("current_tokens")) + self._to_int(
            final.get("current")
        )
        totals["total_tokens"] = self._to_int(totals.get("total_tokens")) + self._to_int(
            final.get("total")
        )
        totals["compaction_passes"] = self._to_int(totals.get("compaction_passes")) + self._to_int(
            stats.get("compactionPasses")
        )
        totals["trimmed_history_messages"] = self._to_int(
            totals.get("trimmed_history_messages")
        ) + self._to_int(stats.get("trimmedHistoryMessages"))

        usage["totals"] = totals
        usage["last"] = stats
        session.metadata["context_usage"] = usage

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        model: str | None = None,
        session_key: str | None = None,
        require_approval: list[str] | None = None,
    ) -> tuple[str | None, list[str], list[dict], dict[str, Any]]:
        """Run the agent loop. Returns (final_content, tools_used, messages, usage)."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        latest_usage: dict[str, Any] = {}
        active_model = model or self.model
        active_provider = self.provider
        if active_model.startswith(("openai-codex/", "openai_codex/")):
            if self._codex_provider is None:
                from nanobot.providers.openai_codex_provider import OpenAICodexProvider

                self._codex_provider = OpenAICodexProvider(default_model=active_model)
            active_provider = self._codex_provider

        while iteration < self.max_iterations:
            iteration += 1

            response = await active_provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=active_model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                reasoning_effort=self.reasoning_effort,
            )
            if isinstance(response.usage, dict):
                latest_usage = response.usage

            if response.has_tool_calls:
                if on_progress:
                    clean = self._strip_think(response.content)
                    if clean:
                        await on_progress(clean)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_call.name, args_str[:200])

                    # Permission check
                    needs_approval = (
                        require_approval
                        and tool_call.name in require_approval
                        and tool_call.name not in self._session_auto_approve.get(session_key or "", set())
                    )
                    if needs_approval and self._permission_callback:
                        if on_progress:
                            await on_progress("", tool_event={
                                "type": "permission_asked",
                                "call_id": tool_call.id,
                                "name": tool_call.name,
                                "input": tool_call.arguments,
                            })
                        try:
                            reply = await asyncio.wait_for(
                                self._permission_callback(
                                    tool_call.name, tool_call.id, tool_call.arguments
                                ),
                                timeout=300,
                            )
                        except asyncio.TimeoutError:
                            reply = "reject"

                        if on_progress:
                            await on_progress("", tool_event={
                                "type": "permission_replied",
                                "call_id": tool_call.id,
                                "name": tool_call.name,
                                "reply": reply,
                            })

                        if reply == "always" and session_key:
                            self._session_auto_approve.setdefault(session_key, set()).add(
                                tool_call.name
                            )
                        if reply == "reject":
                            result = f"Error: Permission denied by user for tool '{tool_call.name}'."
                            messages = self.context.add_tool_result(
                                messages, tool_call.id, tool_call.name, result
                            )
                            continue

                    # Emit tool-start event
                    if on_progress:
                        await on_progress("", tool_event={
                            "type": "tool_start",
                            "call_id": tool_call.id,
                            "name": tool_call.name,
                            "input": tool_call.arguments,
                        })

                    result = await self.tools.execute(tool_call.name, tool_call.arguments)

                    # Emit tool-done event (with diff metadata for file tools)
                    tool_done_event: dict[str, Any] = {
                        "type": "tool_done",
                        "call_id": tool_call.id,
                        "name": tool_call.name,
                        "input": tool_call.arguments,
                        "output": result[:500] if isinstance(result, str) else str(result)[:500],
                    }
                    if tool_call.name in ("write_file", "edit_file"):
                        tool_obj = self.tools.get(tool_call.name)
                        if tool_obj and hasattr(tool_obj, "last_diff") and tool_obj.last_diff:
                            tool_done_event["diff"] = tool_obj.last_diff
                            tool_obj.last_diff = None
                    if on_progress:
                        await on_progress("", tool_event=tool_done_event)

                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                clean = self._strip_think(response.content)
                # Don't persist error responses to session history — they can
                # poison the context and cause permanent 400 loops (#1303).
                if response.finish_reason == "error":
                    logger.error("LLM returned error: {}", (clean or "")[:200])
                    final_content = clean or "Sorry, I encountered an error calling the AI model."
                    break
                messages = self.context.add_assistant_message(
                    messages,
                    clean,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )
                final_content = clean
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages, latest_usage

    async def run(self) -> None:
        """Run the agent loop, dispatching messages as tasks to stay responsive to /stop."""
        self._running = True
        await self._connect_mcp()
        if self._subconscious:
            await self._subconscious.initialize()
            self._subconscious.set_provider(self.provider)
            self._subconscious.start_background_task()
        logger.info("Agent loop started")

        while self._running:
            try:
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if msg.content.strip().lower() == "/stop":
                await self._handle_stop(msg)
            else:
                task = asyncio.create_task(self._dispatch(msg))
                self._active_tasks.setdefault(msg.session_key, []).append(task)
                task.add_done_callback(
                    lambda t, k=msg.session_key: self._active_tasks.get(k, [])
                    and self._active_tasks[k].remove(t)
                    if t in self._active_tasks.get(k, [])
                    else None
                )

    async def _handle_stop(self, msg: InboundMessage) -> None:
        """Cancel all active tasks and subagents for the session."""
        tasks = self._active_tasks.pop(msg.session_key, [])
        cancelled = sum(1 for t in tasks if not t.done() and t.cancel())
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        sub_cancelled = await self.subagents.cancel_by_session(msg.session_key)
        total = cancelled + sub_cancelled
        content = f"⏹ Stopped {total} task(s)." if total else "No active task to stop."
        await self.bus.publish_outbound(
            OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                session_key=msg.session_key,
            )
        )

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Process a message under the global lock."""
        async with self._processing_lock:
            try:
                response = await self._process_message(msg)
                if response is not None:
                    await self.bus.publish_outbound(response)
                elif msg.channel == "cli":
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="",
                            metadata=msg.metadata or {},
                            session_key=msg.session_key,
                        )
                    )
            except asyncio.CancelledError:
                logger.info("Task cancelled for session {}", msg.session_key)
                raise
            except Exception:
                logger.exception("Error processing message for session {}", msg.session_key)
                await self.bus.publish_outbound(
                    OutboundMessage(
                        channel=msg.channel,
                        chat_id=msg.chat_id,
                        content="Sorry, I encountered an error.",
                        session_key=msg.session_key,
                    )
                )

    async def close_mcp(self) -> None:
        """Close MCP connections and subconscious service."""
        if self._subconscious:
            await self._subconscious.close()
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    async def _recall_memories(self, query: str, prev_assistant: str | None = None) -> str | None:
        """Auto-recall relevant memories, gated by a fast classifier."""
        if not self._subconscious or not self._subconscious_config:
            return None
        try:
            if not await self._subconscious.should_inject(query, prev_assistant):
                logger.info("Memory classifier: skip injection")
                return None
            result = await self._subconscious.recall(
                query,
                budget=self._subconscious_config.auto_inject_budget,
                n=self._subconscious_config.auto_inject_results,
            )
            if result:
                compact = result.replace("\n", "\\n").replace("\t", "\\t")
                logger.info("Memory recall ({} chars): {}", len(result), compact[:300])
            return result or None
        except Exception:
            logger.debug("Memory recall failed, continuing without")
            return None

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        model: str | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (
                msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id)
            )
            logger.info("Processing system message from {}", msg.sender_id)
            key = f"{channel}:{chat_id}"
            active_model = model or self.model
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=self.memory_window)
            # System messages are internal (cron, heartbeat routing) — skip memory
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content,
                channel=channel,
                chat_id=chat_id,
            )
            final_content, _, all_msgs, usage = await self._run_agent_loop(
                messages, model=active_model
            )
            self._save_turn(session, all_msgs, 1 + len(history), usage=usage, model=active_model)
            # Don't feed system messages to subconscious (handled in _save_turn via key check)
            self.sessions.save(session)
            self._last_llm_usage[key] = usage
            return OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content=final_content or "Background task completed.",
                session_key=key,
            )

        compact_in = msg.content.replace("\n", "\\n").replace("\t", "\\t")
        preview = compact_in[:80] + "..." if len(compact_in) > 80 else compact_in
        logger.info("Processing message from {}:{}: {}", msg.channel, msg.sender_id, preview)

        key = session_key or msg.session_key
        session = self.sessions.get_or_create(key)
        active_model = model or self.model

        # Slash commands
        cmd = msg.content.strip().lower()
        if cmd == "/clear":
            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="Session cleared.",
                session_key=key,
            )
        if cmd == "/help":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="🐈 nanobot commands:\n/clear — Clear session history\n/compact — Summarize and compact context\n/stop — Stop the current task\n/help — Show available commands",
                session_key=key,
            )

        unconsolidated = len(session.messages) - session.last_consolidated
        if unconsolidated >= self.memory_window and session.key not in self._consolidating:
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.key)
                    _task = asyncio.current_task()
                    if _task is not None:
                        self._consolidation_tasks.discard(_task)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks.add(_task)

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        is_heartbeat = key == "heartbeat"

        if is_heartbeat:
            if "title" not in session.metadata:
                session.metadata["title"] = "Heartbeat"
            # Keep last 5 user/assistant exchanges for context on recent heartbeats,
            # but strip tool_calls/tool messages that incompatible models reject.
            _HB_KEEP = 5
            raw = session.get_history(max_messages=self.memory_window)
            pairs: list[dict] = []
            for m in raw:
                role = m.get("role")
                if role == "user":
                    pairs.append(m)
                elif role == "assistant" and m.get("content") and not m.get("tool_calls"):
                    pairs.append(m)
                # skip tool / tool_calls-only assistant messages
            # Take last N pairs (each pair = user + assistant)
            assistant_count = sum(1 for m in pairs if m.get("role") == "assistant")
            if assistant_count > _HB_KEEP:
                trim = assistant_count - _HB_KEEP
                trimmed: list[dict] = []
                seen = 0
                for m in pairs:
                    if m.get("role") == "assistant":
                        seen += 1
                        if seen <= trim:
                            continue
                    elif seen < trim:
                        continue
                    trimmed.append(m)
                pairs = trimmed
            history = pairs
        else:
            history = session.get_history(max_messages=self.memory_window)
        relevant_memories: str | None = None
        if not is_heartbeat:
            prev_assistant = next(
                (m.get("content") for m in reversed(history) if m.get("role") == "assistant" and isinstance(m.get("content"), str)),
                None,
            )
            relevant_memories = await self._recall_memories(msg.content, prev_assistant)

        # Token-aware context compaction before requesting the model.
        initial_usage: dict[str, int] | None = None
        final_usage: dict[str, int] | None = None
        compaction_passes = 0
        trimmed_history_messages = 0

        for attempt in range(3):
            probe = self.context.build_messages(
                history=history,
                current_message=msg.content,
                media=msg.media if msg.media else None,
                channel=msg.channel,
                chat_id=msg.chat_id,
                relevant_memories=relevant_memories,
            )
            usage = self._context_usage_breakdown(probe, active_model)
            if initial_usage is None:
                initial_usage = usage
            budget = self._context_budget()
            logger.info(
                "Context usage [{}]: total={} (system={}, history={}, current={}) / budget={} (ctx={}, reserve={}, max_out={})",
                active_model,
                usage["total"],
                usage["system"],
                usage["history"],
                usage["current"],
                budget,
                self.context_tokens,
                self.reserve_tokens_floor,
                self.max_tokens,
            )

            if usage["total"] <= budget:
                break

            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._consolidating.add(session.key)
            try:
                async with lock:
                    if not await self._consolidate_memory(session):
                        break
                    self.sessions.save(session)
                    history = session.get_history(max_messages=self.memory_window)
                    compaction_passes += 1
                    logger.info("Compaction pass {} applied for {}", attempt + 1, session.key)
            finally:
                self._consolidating.discard(session.key)

        preview = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            relevant_memories=relevant_memories,
        )
        if self._count_tokens(preview, active_model) > self._context_budget() and history:
            before_trim = len(history)
            history = self._trim_history_by_budget(
                history,
                current_message=msg.content,
                media=msg.media,
                channel=msg.channel,
                chat_id=msg.chat_id,
                model=active_model,
                relevant_memories=relevant_memories,
            )
            trimmed_history_messages = max(0, before_trim - len(history))

        final_preview = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            relevant_memories=relevant_memories,
        )
        final_usage = self._context_usage_breakdown(final_preview, active_model)
        budget = self._context_budget()
        self._last_context_stats[key] = {
            "model": active_model,
            "budget": budget,
            "contextTokens": self.context_tokens,
            "reserveTokensFloor": self.reserve_tokens_floor,
            "maxOutputTokens": self.max_tokens,
            "initial": initial_usage or final_usage,
            "final": final_usage,
            "compactionPasses": compaction_passes,
            "trimmedHistoryMessages": trimmed_history_messages,
            "withinBudget": final_usage["total"] <= budget,
            "usagePercent": round((final_usage["total"] / budget) * 100, 2) if budget > 0 else 0.0,
        }
        self._record_context_usage(session, self._last_context_stats[key])

        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
            relevant_memories=relevant_memories,
        )

        async def _bus_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_event: dict | None = None,
        ) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            if tool_event:
                meta["_tool_event"] = tool_event
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=msg.channel,
                    chat_id=msg.chat_id,
                    content=content,
                    metadata=meta,
                    session_key=key,
                )
            )

        final_content, _, all_msgs, usage = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            model=active_model,
            session_key=key,
            require_approval=self._require_approval or None,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        self._save_turn(session, all_msgs, 1 + len(history), usage=usage, model=active_model)
        self.sessions.save(session)
        self._last_llm_usage[key] = usage

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        compact_out = final_content.replace("\n", "\\n").replace("\t", "\\t")
        preview = compact_out[:120] + "..." if len(compact_out) > 120 else compact_out
        logger.info("Response to {}:{}: {}", msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},
            session_key=key,
        )

    def _save_turn(
        self,
        session: Session,
        messages: list[dict],
        skip: int,
        usage: dict[str, Any] | None = None,
        model: str | None = None,
    ) -> None:
        """Save new-turn messages into session, truncating large tool results."""
        from datetime import datetime

        saved_entries: list[dict[str, Any]] = []

        for m in messages[skip:]:
            entry = dict(m)
            role, content = entry.get("role"), entry.get("content")
            if role == "assistant" and not content and not entry.get("tool_calls"):
                continue  # skip empty assistant messages — they poison session context
            if (
                role == "tool"
                and isinstance(content, str)
                and len(content) > self._TOOL_RESULT_MAX_CHARS
            ):
                entry["content"] = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            elif role == "user":
                if isinstance(content, str):
                    # Strip the runtime-context prefix
                    if content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                        parts = content.split("\n\n", 1)
                        content = parts[1].strip() if len(parts) > 1 else ""
                    # Strip recalled memories suffix
                    mem_tag = ContextBuilder._MEMORY_CONTEXT_TAG
                    if mem_tag in content:
                        content = content[: content.index(mem_tag)].strip()
                    if not content:
                        continue
                    entry["content"] = content
                if isinstance(content, list):
                    filtered = []
                    for c in content:
                        if not isinstance(c, dict):
                            continue
                        text = c.get("text", "") if c.get("type") == "text" else None
                        if text is not None and (
                            text.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG)
                            or text.startswith(ContextBuilder._MEMORY_CONTEXT_TAG)
                        ):
                            continue
                        if c.get("type") == "image_url" and c.get("image_url", {}).get(
                            "url", ""
                        ).startswith("data:image/"):
                            filtered.append({"type": "text", "text": "[image]"})
                        else:
                            filtered.append(c)
                    if not filtered:
                        continue
                    entry["content"] = filtered
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
            saved_entries.append(entry)

        if isinstance(usage, dict) and saved_entries:
            target: dict[str, Any] | None = None
            for entry in reversed(saved_entries):
                if entry.get("role") == "assistant" and not entry.get("tool_calls"):
                    target = entry
                    break
            if target is None:
                for entry in reversed(saved_entries):
                    if entry.get("role") == "assistant":
                        target = entry
                        break
            if target is not None:
                target["usage"] = dict(usage)
                if model:
                    target["model"] = model

        session.updated_at = datetime.now()

        # Feed new messages to subconscious for extraction (skip heartbeat/system tasks)
        if self._subconscious and session.key != "heartbeat":
            self._subconscious.feed_messages(messages[skip:], session_key=session.key)

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """Consolidate session history by trimming old messages.

        When subconscious is active, extraction happens continuously so
        consolidation only needs to trim the session. Falls back to the
        legacy MemoryStore consolidation when subconscious is disabled.
        """
        if self._subconscious:
            # Subconscious handles extraction; just trim session messages
            if archive_all:
                session.last_consolidated = len(session.messages)
                return True
            keep_count = self.memory_window // 2
            if len(session.messages) <= keep_count:
                return True
            session.last_consolidated = len(session.messages) - keep_count
            return True

        # Legacy fallback
        from nanobot.agent.memory import MemoryStore

        return await MemoryStore(self.workspace).consolidate(
            session,
            self.provider,
            self.model,
            archive_all=archive_all,
            memory_window=self.memory_window,
        )

    async def compact_session(
        self, session_key: str, *, archive_all: bool = False
    ) -> dict[str, Any]:
        """Force memory compaction for a session and return compaction status."""
        session = self.sessions.get_or_create(session_key)
        lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
        self._consolidating.add(session.key)
        try:
            async with lock:
                before = session.last_consolidated
                success = await self._consolidate_memory(session, archive_all=archive_all)
                self.sessions.save(session)
                return {
                    "ok": bool(success),
                    "archiveAll": archive_all,
                    "lastConsolidatedBefore": before,
                    "lastConsolidatedAfter": session.last_consolidated,
                    "messageCount": len(session.messages),
                }
        finally:
            self._consolidating.discard(session.key)

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
        model: str | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        if self._subconscious and not self._subconscious._qmd.available:
            await self._subconscious.initialize()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(
            msg, session_key=session_key, on_progress=on_progress, model=model
        )
        return response.content if response else ""
