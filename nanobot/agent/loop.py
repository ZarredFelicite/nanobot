"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import weakref
from datetime import datetime
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.subagent import SubagentTool
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider, ToolCallRequest
from nanobot.security.prompt_injection import validate_model_output
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import Config
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

    _TOOL_RESULT_MAX_CHARS = 30_000
    _LLM_RETRY_MAX_ATTEMPTS = 3
    _LLM_RETRY_BASE_DELAY_S = 1.0
    _LLM_RETRY_MAX_DELAY_S = 8.0

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        config: Config | None = None,
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
        node_registry: Any | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig

        self.bus = bus
        self._config = config
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
        self._model_limit_cache: dict[str, dict[str, Any]] = {}
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

            self._subconscious = SubconsciousService(
                workspace,
                subconscious_config,
                provider_factory=self._build_provider_for_model,
                fallback_models=config.models.fallbacks if config else None,
            )

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
        self._node_registry = node_registry
        self._owner_message_target = self._resolve_owner_message_target(channels_config)
        self._register_default_tools()

    @staticmethod
    def _resolve_owner_message_target(
        channels_config: ChannelsConfig | None,
    ) -> tuple[str, str] | None:
        """Resolve fixed owner routing target from channel config."""
        if not channels_config:
            return None

        allow_from = channels_config.telegram.allow_from
        for raw in allow_from:
            value = raw.strip()
            if not value or value == "*":
                continue
            owner_id = value.split("|", 1)[0].strip()
            if owner_id:
                return ("telegram", owner_id)
        return None

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
                untrusted_programs=self.exec_config.untrusted_programs,
            )
        )
        self.tools.register(WebSearchTool(api_key=self.brave_api_key, proxy=self.web_proxy))
        self.tools.register(WebFetchTool(proxy=self.web_proxy))
        owner_channel = ""
        owner_chat_id = ""
        if self._owner_message_target:
            owner_channel, owner_chat_id = self._owner_message_target

        self.tools.register(
            MessageTool(
                send_callback=self.bus.publish_outbound,
                owner_channel=owner_channel,
                owner_chat_id=owner_chat_id,
            )
        )
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(
                CronTool(
                    self.cron_service,
                    owner_channel=owner_channel,
                    owner_chat_id=owner_chat_id,
                )
            )
        if self._subconscious:
            from nanobot.agent.tools.memory_recall import MemoryRecallTool

            self.tools.register(MemoryRecallTool(self._subconscious))
        from nanobot.agent.tools.tasks import TaskTool

        self.tools.register(TaskTool(workspace=self.workspace))
        self.tools.register(SubagentTool(workspace=self.workspace))
        if self._node_registry:
            from nanobot.agent.tools.remote_exec import RemoteExecTool

            self.tools.register(RemoteExecTool(self._node_registry))

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

    _INVALID_TOOL_CALL_NAME = "invalid_tool_call"

    @classmethod
    def _normalize_tool_call_name(cls, name: Any) -> str:
        """Return a safe printable tool name for malformed tool calls."""
        if isinstance(name, str) and name.strip():
            return name.strip()
        return cls._INVALID_TOOL_CALL_NAME

    @classmethod
    def _messages_for_model(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Strip synthetic malformed-tool placeholders before the next model call."""
        cleaned: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "tool" and msg.get("name") == cls._INVALID_TOOL_CALL_NAME:
                continue
            if role == "assistant" and msg.get("tool_calls"):
                tool_calls = msg.get("tool_calls") or []
                filtered_tool_calls = []
                for tc in tool_calls:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    if fn.get("name") == cls._INVALID_TOOL_CALL_NAME:
                        continue
                    filtered_tool_calls.append(tc)
                if len(filtered_tool_calls) != len(tool_calls):
                    clean = dict(msg)
                    if filtered_tool_calls:
                        clean["tool_calls"] = filtered_tool_calls
                    else:
                        clean.pop("tool_calls", None)
                        if not clean.get("content") and not clean.get("reasoning_content"):
                            continue
                    cleaned.append(clean)
                    continue
            cleaned.append(msg)
        return cleaned

    @classmethod
    def _tool_hint(cls, tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""

        # Keys likely to be the most informative arg for display
        _DESCRIPTIVE_KEYS = (
            "title",
            "query",
            "path",
            "file_path",
            "command",
            "content",
            "url",
            "name",
            "message",
        )
        # Keys that are structural/less informative
        _SKIP_KEYS = ("action", "type", "group", "status")

        def _fmt(tc):
            tc_name = cls._normalize_tool_call_name(getattr(tc, "name", None))
            args = (tc.arguments[0] if isinstance(tc.arguments, list) else tc.arguments) or {}
            if not isinstance(args, dict):
                return tc_name

            # Pick the most descriptive string argument
            val = None
            for key in _DESCRIPTIVE_KEYS:
                if key in args and isinstance(args[key], str):
                    val = args[key]
                    break
            if val is None:
                # Fall back to first string value that isn't structural
                for k, v in args.items():
                    if isinstance(v, str) and k not in _SKIP_KEYS:
                        val = v
                        break
            if val is None:
                val = next((v for v in args.values() if isinstance(v, str)), None)

            if not isinstance(val, str):
                return tc_name
            return f'{tc_name}("{val[:60]}…")' if len(val) > 60 else f'{tc_name}("{val}")'

        return ", ".join(_fmt(tc) for tc in tool_calls)

    @staticmethod
    def _extract_markup_tool_calls(content: str | None, iteration: int) -> list[ToolCallRequest]:
        """Extract pseudo-XML tool calls emitted as plain text.

        Some providers/models occasionally return tool calls formatted like:
        <tool_call><function=message>...<parameter=foo>bar</parameter>...</function></tool_call>
        instead of structured tool_calls. Parse those blocks so the agent can
        execute tools instead of treating them as final text.
        """
        if not content or "<tool_call>" not in content:
            return []

        calls: list[ToolCallRequest] = []
        blocks = re.findall(r"<tool_call>([\s\S]*?)</tool_call>", content)
        for i, block in enumerate(blocks):
            fn_match = re.search(r"<function=([a-zA-Z0-9_-]+)>", block)
            if not fn_match:
                continue
            name = fn_match.group(1)

            args: dict[str, Any] = {}
            for param_match in re.finditer(
                r"<parameter=([a-zA-Z0-9_-]+)>\s*([\s\S]*?)\s*</parameter>", block
            ):
                key = param_match.group(1)
                value = param_match.group(2).strip()
                args[key] = value

            calls.append(ToolCallRequest(id=f"markup_{iteration}_{i}", name=name, arguments=args))

        return calls

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

    async def _refresh_model_limits(self, model: str) -> None:
        """Fetch provider-reported limits for the active model when available."""
        if not model:
            return
        if model in self._model_limit_cache:
            return
        try:
            provider = self.get_provider_for_model(model)
            getter = getattr(provider, "get_model_limits", None)
            if not callable(getter):
                self._model_limit_cache[model] = {}
                return
            limits = await getter(model)
            if limits is None:
                self._model_limit_cache[model] = {}
                return
            self._model_limit_cache[model] = {
                "context_tokens": limits.context_tokens,
                "max_output_tokens": limits.max_output_tokens,
                "metadata": limits.metadata,
            }
        except Exception:
            logger.exception("Failed to refresh model limits for {}", model)
            self._model_limit_cache[model] = {}

    def _effective_context_tokens(self, model: str | None = None) -> int:
        cached = self._model_limit_cache.get(model or "", {})
        value = cached.get("context_tokens") if isinstance(cached, dict) else None
        if isinstance(value, int) and value > 0:
            return max(4096, value)
        return self.context_tokens

    def _context_budget(self, model: str | None = None) -> int:
        """Maximum prompt tokens before compaction is required."""
        return max(512, self._effective_context_tokens(model) - self.reserve_tokens_floor)

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

    def _should_background_compact(
        self,
        session: Session,
        *,
        channel: str,
        chat_id: str,
        model: str,
    ) -> bool:
        """Decide whether to proactively compact based on actual token usage."""
        history = session.get_history(max_messages=max(self.memory_window, len(session.messages)))
        if not history:
            return False

        probe = self.context.build_messages(
            history=history,
            current_message="",
            channel=channel,
            chat_id=chat_id,
        )
        usage = self._context_usage_breakdown(probe, model)
        trigger_budget = max(512, int(self._context_budget(model) * 0.9))
        should_compact = usage["total"] >= trigger_budget
        if should_compact:
            logger.info(
                "Background compaction triggered by token usage for {}: total={} threshold={} history_messages={}",
                session.key,
                usage["total"],
                trigger_budget,
                len(history),
            )
        return should_compact

    def _context_component_breakdown(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        media: list[str] | None,
        channel: str,
        chat_id: str,
        model: str,
        relevant_memories: str | None = None,
    ) -> dict[str, Any]:
        """Estimate prompt token usage by major source category."""
        preview = self.context.build_messages(
            history=history,
            current_message=current_message,
            media=media if media else None,
            channel=channel,
            chat_id=chat_id,
            relevant_memories=relevant_memories,
        )
        total = self._count_tokens(preview, model)
        sections = self.context.build_system_prompt_sections()
        system_prompt_tokens = self._count_tokens(
            [{"role": "system", "content": sections.get("base", "")}], model
        )
        full_system_tokens = self._count_tokens(
            [{"role": "system", "content": sections.get("full", "")}], model
        )
        skills_tokens = max(0, full_system_tokens - system_prompt_tokens)
        tool_messages = [m for m in preview[1:-1] if m.get("role") == "tool"]
        tool_output_tokens = self._count_tokens(tool_messages, model) if tool_messages else 0
        messages_tokens = max(0, total - full_system_tokens - tool_output_tokens)
        return {
            "total": total,
            "systemPrompt": system_prompt_tokens,
            "skills": skills_tokens,
            "toolOutputs": tool_output_tokens,
            "messages": messages_tokens,
        }

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
        budget = self._context_budget(model)

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

    def estimate_context_stats(
        self,
        session: Session,
        *,
        channel: str = "opencode",
        chat_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Estimate current context usage for a session outside an active turn."""
        active_model = model or session.metadata.get("model") or self.model
        history = session.get_history(max_messages=max(self.memory_window, len(session.messages)))
        preview = self.context.build_messages(
            history=history,
            current_message="",
            channel=channel,
            chat_id=chat_id or session.key,
        )
        final_usage = self._context_usage_breakdown(preview, active_model)
        budget = self._context_budget(active_model)
        return {
            "model": active_model,
            "budget": budget,
            "contextTokens": self._effective_context_tokens(active_model),
            "reserveTokensFloor": self.reserve_tokens_floor,
            "initial": final_usage,
            "final": final_usage,
            "breakdown": self._context_component_breakdown(
                history=history,
                current_message="",
                media=None,
                channel=channel,
                chat_id=chat_id or session.key,
                model=active_model,
                relevant_memories=None,
            ),
            "compactionPasses": 0,
            "trimmedHistoryMessages": 0,
            "withinBudget": final_usage["total"] <= budget,
            "usagePercent": round((final_usage["total"] / budget) * 100, 2) if budget > 0 else 0.0,
        }

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

    def get_provider_for_model(self, model: str) -> "LLMProvider":
        """Return the appropriate provider for a given model string.

        Uses the codex provider for openai-codex/* models and the default
        litellm provider for everything else.  When the default provider
        itself is a codex provider (because the default model is codex),
        creates a litellm fallback for non-codex models.
        """
        if model.startswith(("openai-codex/", "openai_codex/")):
            if self._codex_provider is None:
                from nanobot.providers.openai_codex_provider import OpenAICodexProvider

                self._codex_provider = OpenAICodexProvider(default_model=model)
            return self._codex_provider

        if self._config is not None:
            return self._build_provider_for_model(model)

        from nanobot.providers.openai_codex_provider import OpenAICodexProvider

        if isinstance(self.provider, OpenAICodexProvider):
            # Default provider is codex but the requested model is not —
            # create a litellm provider that relies on env-var API keys.
            if not hasattr(self, "_litellm_fallback") or self._litellm_fallback is None:
                from nanobot.providers.litellm_provider import LiteLLMProvider

                self._litellm_fallback = LiteLLMProvider(default_model=model)
            return self._litellm_fallback
        return self.provider

    def _build_provider_for_model(self, model: str) -> "LLMProvider":
        """Build a provider for a specific model using runtime config."""
        if self._config is None:
            return self.get_provider_for_model(model)

        from nanobot.providers.factory import make_provider

        return make_provider(self._config, model_override=model, current_provider=self.provider)

    @classmethod
    def _is_retryable_model_error(cls, content: str | None) -> bool:
        """Return True for transient model/provider failures worth retrying."""
        if not content:
            return True

        text = content.strip().lower()
        if not text:
            return True

        retryable_markers = (
            "timeout",
            "timed out",
            "readtimeout",
            "connection reset",
            "connection refused",
            "connection aborted",
            "connection error",
            "remote connection failure",
            "transport failure",
            "temporarily unavailable",
            "service unavailable",
            "server error",
            "internal server error",
            "bad gateway",
            "gateway timeout",
            "rate limit",
            "too many requests",
            "overloaded",
            "upstream connect error",
            "network error",
            "name or service not known",
        )
        if any(marker in text for marker in retryable_markers):
            return True

        ambiguous_prefixes = (
            "error calling codex:",
            "error calling llm:",
        )
        return any(text == prefix or text.endswith(prefix) for prefix in ambiguous_prefixes)

    @classmethod
    def _is_quota_error(cls, content: str | None) -> bool:
        """Return True for quota/subscription exhaustion — should try next model, not retry."""
        if not content:
            return False
        text = content.strip().lower()
        quota_markers = (
            "quota exceeded",
            "usage quota",
            "subscription",
            "billing",
            "insufficient_quota",
            "exceeded your current quota",
            "plan limit",
            "spending limit",
            "budget exceeded",
        )
        return any(marker in text for marker in quota_markers)

    def _get_fallback_models(self, exclude_model: str) -> list[str]:
        """Get fallback models from config, excluding the failed model."""
        if self._config is None:
            return []
        fallbacks = self._config.models.fallbacks
        return [m for m in fallbacks if m != exclude_model]

    async def _chat_with_model_fallback(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> tuple["LLMResponse", str, "LLMProvider"]:
        """Try primary model, then fallback models on failure.

        Returns (response, active_model, active_provider) so the caller can
        update which model/provider to use for subsequent iterations.
        """
        provider = self.get_provider_for_model(model)

        # Try primary model
        response = await self._chat_with_retry(
            provider,
            messages=messages,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
            on_text_delta=on_text_delta,
            on_reasoning_delta=on_reasoning_delta,
        )
        if not response.is_error:
            return response, model, provider

        # Try fallback models
        fallbacks = self._get_fallback_models(model)
        if not fallbacks:
            return response, model, provider

        logger.warning(
            "Primary model {} failed: {}. Trying {} fallback model(s)...",
            model,
            (response.content or "")[:200],
            len(fallbacks),
        )

        for fb_model in fallbacks:
            fb_provider = self.get_provider_for_model(fb_model)
            fb_response = await self._chat_with_retry(
                fb_provider,
                messages=messages,
                tools=tools,
                model=fb_model,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                on_text_delta=on_text_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
            if not fb_response.is_error:
                logger.info("Fallback model {} succeeded", fb_model)
                return fb_response, fb_model, fb_provider
            logger.warning(
                "Fallback model {} also failed: {}",
                fb_model,
                (fb_response.content or "")[:100],
            )

        # All fallbacks exhausted — return last error
        logger.error("All fallback models exhausted")
        return response, model, provider

    async def _chat_with_retry(
        self,
        provider: "LLMProvider",
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
    ):
        """Call provider.chat with exponential backoff for transient failures."""
        delay = self._LLM_RETRY_BASE_DELAY_S
        last_response = None

        for attempt in range(1, self._LLM_RETRY_MAX_ATTEMPTS + 1):
            if on_text_delta and provider.__class__.stream_chat is not LLMProvider.stream_chat:
                response = await provider.stream_chat(
                    messages=messages,
                    tools=tools,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    on_text_delta=on_text_delta,
                    on_reasoning_delta=on_reasoning_delta,
                )
            else:
                response = await provider.chat(
                    messages=messages,
                    tools=tools,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                )
            last_response = response
            if not response.is_error or attempt >= self._LLM_RETRY_MAX_ATTEMPTS:
                return response
            if response.streamed_content:
                return response
            if self._is_quota_error(response.content):
                return response  # Quota errors won't recover with retries
            if not self._is_retryable_model_error(response.content):
                return response

            logger.warning(
                "Transient LLM error on attempt {}/{} for model {}: {}. Retrying in {:.1f}s",
                attempt,
                self._LLM_RETRY_MAX_ATTEMPTS,
                model,
                (response.content or "")[:200],
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._LLM_RETRY_MAX_DELAY_S)

        return last_response

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        model: str | None = None,
        session_key: str | None = None,
        require_approval: list[str] | None = None,
        on_step: Callable[[list[dict]], Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict], dict[str, Any]]:
        """Run the agent loop. Returns (final_content, tools_used, messages, usage)."""
        messages = initial_messages
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        latest_usage: dict[str, Any] = {}
        last_tool_batch_signature: str | None = None
        repeated_tool_batch_count = 0
        active_model = model or self.model
        active_provider = self.get_provider_for_model(active_model)

        while iteration < self.max_iterations:
            iteration += 1
            prev_len = len(messages)  # track messages added this iteration for on_step

            async def _on_text_delta(delta: str) -> None:
                if on_progress:
                    await on_progress(delta)

            async def _on_reasoning_delta(delta: str) -> None:
                if on_progress:
                    await on_progress(delta, is_reasoning=True)

            response, active_model, active_provider = await self._chat_with_model_fallback(
                messages=self._messages_for_model(messages),
                tools=self.tools.get_definitions(),
                model=active_model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                reasoning_effort=self.reasoning_effort,
                on_text_delta=_on_text_delta if on_progress else None,
                on_reasoning_delta=_on_reasoning_delta if on_progress else None,
            )
            if isinstance(response.usage, dict):
                latest_usage = response.usage

            parsed_markup_calls: list[ToolCallRequest] = []
            if not response.has_tool_calls:
                parsed_markup_calls = self._extract_markup_tool_calls(response.content, iteration)
                if parsed_markup_calls:
                    logger.warning(
                        "Provider returned pseudo-markup tool call(s); executing parsed calls: {}",
                        ", ".join(self._normalize_tool_call_name(tc.name) for tc in parsed_markup_calls),
                    )

            effective_tool_calls = response.tool_calls or parsed_markup_calls

            if effective_tool_calls:
                malformed_tool_calls = [
                    tc for tc in effective_tool_calls
                    if self._normalize_tool_call_name(tc.name) == self._INVALID_TOOL_CALL_NAME
                ]
                valid_tool_calls = [
                    tc for tc in effective_tool_calls
                    if self._normalize_tool_call_name(tc.name) != self._INVALID_TOOL_CALL_NAME
                ]

                # Safety: if the model emits the exact same tool-call batch
                # repeatedly, stop early to avoid spinning forever.
                batch_signature = json.dumps(
                    [
                        {
                            "name": self._normalize_tool_call_name(tc.name),
                            "arguments": tc.arguments,
                        }
                        for tc in effective_tool_calls
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                if batch_signature == last_tool_batch_signature:
                    repeated_tool_batch_count += 1
                else:
                    repeated_tool_batch_count = 1
                    last_tool_batch_signature = batch_signature

                max_repeats = 1 if malformed_tool_calls and not valid_tool_calls else (3 if session_key == "heartbeat" else 4)
                if repeated_tool_batch_count >= max_repeats:
                    logger.warning(
                        "Stopping tool loop after {} repeated identical tool-call batches (session={})",
                        repeated_tool_batch_count,
                        session_key,
                    )
                    final_content = response.content or ""
                    break

                if on_progress:
                    # Send reasoning and pre-tool text as thinking, before tool calls.
                    if response.reasoning_content:
                        await on_progress(response.reasoning_content, is_reasoning=True)
                    clean = self._strip_think(response.content)
                    if parsed_markup_calls:
                        clean = None
                    if clean and not response.streamed_content:
                        await on_progress(clean, is_reasoning=True)
                    await on_progress(self._tool_hint(effective_tool_calls), tool_hint=True)

                if malformed_tool_calls:
                    available_tools = ", ".join(self.tools.tool_names)
                    retry_msg = (
                        "Your last tool call was malformed because function.name was empty/null or invalid. "
                        f"Retry immediately with a valid tool call using one of these exact tool names: {available_tools}. "
                        "Do not call invalid_tool_call."
                    )
                    messages.append({"role": "user", "content": retry_msg})

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": self._normalize_tool_call_name(tc.name),
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in effective_tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                    thinking_blocks=response.thinking_blocks,
                )

                # Batched parallel + sequential tool execution
                parallel_batch: list[ToolCallRequest] = []
                max_parallel = getattr(self, "_max_parallel_tools", 8)
                _sem = asyncio.Semaphore(max_parallel)

                async def _exec_one(tc: ToolCallRequest) -> tuple[ToolCallRequest, str]:
                    async with _sem:
                        tc_name = self._normalize_tool_call_name(tc.name)
                        if on_progress:
                            await on_progress(
                                "",
                                tool_event={
                                    "type": "tool_start",
                                    "call_id": tc.id,
                                    "name": tc_name,
                                    "input": tc.arguments,
                                },
                            )
                        r = await self.tools.execute(tc_name, tc.arguments)
                        return tc, r

                async def _flush_parallel(
                    batch: list[ToolCallRequest], msgs: list[dict]
                ) -> list[dict]:
                    if not batch:
                        return msgs
                    results = await asyncio.gather(*[_exec_one(tc) for tc in batch])
                    for tc, result in results:
                        tc_name = self._normalize_tool_call_name(tc.name)
                        tool_done_event: dict[str, Any] = {
                            "type": "tool_done",
                            "call_id": tc.id,
                            "name": tc_name,
                            "input": tc.arguments,
                            "output": result[:500]
                            if isinstance(result, str)
                            else str(result)[:500],
                        }
                        if on_progress:
                            await on_progress("", tool_event=tool_done_event)
                        msgs = self.context.add_tool_result(msgs, tc.id, tc_name, result)
                    return msgs

                for tool_call in effective_tool_calls:
                    tool_name = self._normalize_tool_call_name(tool_call.name)
                    tools_used.append(tool_name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call: {}({})", tool_name, args_str[:200])

                    # Permission check
                    needs_approval = (
                        require_approval
                        and tool_name in require_approval
                        and tool_name not in self._session_auto_approve.get(session_key or "", set())
                    )

                    tool_obj = self.tools.get(tool_name)
                    is_parallel_safe = (
                        tool_obj is not None and tool_obj.parallel_safe and not needs_approval
                    )

                    if is_parallel_safe:
                        parallel_batch.append(tool_call)
                        continue

                    # Flush any pending parallel batch before sequential execution
                    messages = await _flush_parallel(parallel_batch, messages)
                    parallel_batch = []

                    if needs_approval and self._permission_callback:
                        if on_progress:
                            await on_progress(
                                "",
                                tool_event={
                                    "type": "permission_asked",
                                    "call_id": tool_call.id,
                                    "name": tool_name,
                                    "input": tool_call.arguments,
                                },
                            )
                        try:
                            reply = await asyncio.wait_for(
                                self._permission_callback(
                                    tool_name, tool_call.id, tool_call.arguments
                                ),
                                timeout=300,
                            )
                        except asyncio.TimeoutError:
                            reply = "reject"

                        if on_progress:
                            await on_progress(
                                "",
                                tool_event={
                                    "type": "permission_replied",
                                    "call_id": tool_call.id,
                                    "name": tool_name,
                                    "reply": reply,
                                },
                            )

                        if reply == "always" and session_key:
                            self._session_auto_approve.setdefault(session_key, set()).add(
                                tool_name
                            )
                        if reply == "reject":
                            result = f"Error: Permission denied by user for tool '{tool_name}'."
                            messages = self.context.add_tool_result(
                                messages, tool_call.id, tool_name, result
                            )
                            continue

                    # Emit tool-start event
                    if on_progress:
                        await on_progress(
                            "",
                            tool_event={
                                "type": "tool_start",
                                "call_id": tool_call.id,
                                "name": tool_name,
                                "input": tool_call.arguments,
                            },
                        )

                    result = await self.tools.execute(tool_name, tool_call.arguments)

                    # Emit tool-done event (with diff metadata for file tools)
                    tool_done_event: dict[str, Any] = {
                        "type": "tool_done",
                        "call_id": tool_call.id,
                        "name": tool_name,
                        "input": tool_call.arguments,
                        "output": result[:500] if isinstance(result, str) else str(result)[:500],
                    }
                    if tool_name in ("write_file", "edit_file"):
                        if tool_obj and hasattr(tool_obj, "last_diff") and tool_obj.last_diff:
                            tool_done_event["diff"] = tool_obj.last_diff
                            tool_obj.last_diff = None
                    if on_progress:
                        await on_progress("", tool_event=tool_done_event)

                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_name, result
                    )

                # Flush any remaining parallel batch
                messages = await _flush_parallel(parallel_batch, messages)

                # Checkpoint: persist messages added this iteration to disk
                if on_step and len(messages) > prev_len:
                    await on_step(messages[prev_len:])
            else:
                clean = self._strip_think(response.content)
                validation = validate_model_output(clean)
                if not validation.safe:
                    logger.warning(
                        "Blocked suspicious model output due to: {}",
                        ", ".join(validation.findings),
                    )
                    clean = validation.replacement
                # Stream thinking and final text to on_progress
                if on_progress:
                    if response.reasoning_content:
                        await on_progress(response.reasoning_content, is_reasoning=True)
                    if clean and not response.streamed_content:
                        await on_progress(clean)
                # Don't persist error responses to session history - they can
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

                # Checkpoint: persist final assistant message to disk
                if on_step and len(messages) > prev_len:
                    await on_step(messages[prev_len:])
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

    def apply_runtime_config(self, config: "Config", provider: LLMProvider) -> None:
        """Apply a freshly loaded runtime config without restarting sessions."""
        self._config = config
        self.channels_config = config.channels
        self.provider = provider
        self.model = config.agents.defaults.model
        self.max_iterations = config.agents.defaults.max_tool_iterations
        self.temperature = config.agents.defaults.temperature
        self.max_tokens = config.agents.defaults.max_tokens
        self.memory_window = config.agents.defaults.memory_window
        self.context_tokens = max(4096, config.agents.defaults.context_tokens)
        self.reserve_tokens_floor = max(0, config.agents.defaults.reserve_tokens_floor)
        self.reasoning_effort = config.agents.defaults.reasoning_effort
        self.brave_api_key = config.tools.web.search.api_key or None
        self.web_proxy = config.tools.web.proxy or None
        self.exec_config = config.tools.exec
        self.restrict_to_workspace = config.tools.restrict_to_workspace
        self._max_parallel_tools = config.tools.max_parallel_tools
        self._mcp_servers = config.tools.mcp_servers

        self.subagents.provider = provider
        self.subagents.model = self.model
        self.subagents.temperature = self.temperature
        self.subagents.max_tokens = self.max_tokens
        self.subagents.reasoning_effort = self.reasoning_effort
        self.subagents.brave_api_key = self.brave_api_key
        self.subagents.web_proxy = self.web_proxy
        self.subagents.exec_config = self.exec_config
        self.subagents.restrict_to_workspace = self.restrict_to_workspace

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
        system_prompt: str | None = None,
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
            await self._refresh_model_limits(active_model)
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

        key = session_key or msg.session_key
        compact_in = msg.content.replace("\n", "\\n").replace("\t", "\\t")
        preview = compact_in[:80] + "..." if len(compact_in) > 80 else compact_in
        logger.info(
            "Processing message session={} from {}:{}: {}",
            key,
            msg.channel,
            msg.sender_id,
            preview,
        )

        session = self.sessions.get_or_create(key)
        active_model = model or self.model
        await self._refresh_model_limits(active_model)

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

        if (
            session.key not in self._consolidating
            and self._should_background_compact(
                session,
                channel=msg.channel,
                chat_id=msg.chat_id,
                model=active_model,
            )
        ):
            self._consolidating.add(session.key)
            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        if await self._consolidate_memory(session, generate_summary=True):
                            self.sessions.save(session)
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

        is_heartbeat = key.startswith("heartbeat")

        if is_heartbeat:
            if "title" not in session.metadata:
                session.metadata["title"] = "Heartbeat"
            # Fresh context each tick — no history.  The heartbeat state file
            # (`memory/heartbeat-state.json`) carries all inter-tick state.
            # Keeping prior turns caused the model to skip checks ("already
            # checked 30 min ago"), producing bare HEARTBEAT_OK responses.
            history: list[dict] = []
        else:
            history = session.get_history(max_messages=self.memory_window)
        relevant_memories: str | None = None
        if not is_heartbeat:
            prev_assistant = next(
                (
                    m.get("content")
                    for m in reversed(history)
                    if m.get("role") == "assistant" and isinstance(m.get("content"), str)
                ),
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
            budget = self._context_budget(active_model)
            logger.info(
                "Context usage [{}]: total={} (system={}, history={}, current={}) / budget={} (ctx={}, reserve={})",
                active_model,
                usage["total"],
                usage["system"],
                usage["history"],
                usage["current"],
                budget,
                self._effective_context_tokens(active_model),
                self.reserve_tokens_floor,
            )

            if usage["total"] <= budget:
                break

            lock = self._consolidation_locks.setdefault(session.key, asyncio.Lock())
            self._consolidating.add(session.key)
            try:
                async with lock:
                    if not await self._consolidate_memory(session, generate_summary=True):
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
        if self._count_tokens(preview, active_model) > self._context_budget(active_model) and history:
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
        budget = self._context_budget(active_model)
        self._last_context_stats[key] = {
            "model": active_model,
            "budget": budget,
            "contextTokens": self._effective_context_tokens(active_model),
            "reserveTokensFloor": self.reserve_tokens_floor,
            "initial": initial_usage or final_usage,
            "final": final_usage,
            "breakdown": self._context_component_breakdown(
                history=history,
                current_message=msg.content,
                media=msg.media,
                channel=msg.channel,
                chat_id=msg.chat_id,
                model=active_model,
                relevant_memories=relevant_memories,
            ),
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

        # Override system prompt when caller provides one (e.g. heartbeat subagent)
        if (
            system_prompt is not None
            and initial_messages
            and initial_messages[0].get("role") == "system"
        ):
            initial_messages[0] = {"role": "system", "content": system_prompt}

        async def _bus_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_event: dict | None = None,
            is_reasoning: bool = False,
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

        is_heartbeat = key.startswith("heartbeat") if key else False

        # Checkpoint the user message immediately so it's on disk before any LLM call.
        # This ensures a killed gateway leaves a recoverable session.
        if not is_heartbeat and session.key != "heartbeat:sub" and initial_messages:
            user_entry = self._clean_entry(initial_messages[-1], is_heartbeat=False)
            if user_entry:
                self.sessions.append_message(session, user_entry)

        async def _on_step(new_msgs: list[dict]) -> None:
            """Checkpoint new messages from one agent-loop iteration to disk."""
            for m in new_msgs:
                entry = self._clean_entry(m, is_heartbeat=is_heartbeat)
                if entry:
                    self.sessions.append_message(session, entry)

        final_content, _, all_msgs, usage = await self._run_agent_loop(
            initial_messages,
            on_progress=on_progress or _bus_progress,
            model=active_model,
            session_key=key,
            require_approval=self._require_approval or None,
            on_step=_on_step if not is_heartbeat else None,
        )

        if final_content is None:
            final_content = (
                "" if is_heartbeat else "I've completed processing but have no response to give."
            )

        # Heartbeat silent-completion sentinel — model included NO_RESPONSE
        if is_heartbeat and "NO_RESPONSE" in final_content:
            final_content = ""

        self._save_turn(session, all_msgs, 1 + len(history), usage=usage, model=active_model)
        self.sessions.save(session)
        self._last_llm_usage[key] = usage

        # F1: Background memory nudge — periodic big-picture review
        if self._subconscious and not is_heartbeat:
            if self._subconscious.increment_nudge_counter():
                self._subconscious.reset_nudge_counter()
                snapshot = list(session.messages)
                asyncio.create_task(self._subconscious.nudge_review(snapshot))

        if (mt := self.tools.get("message")) and isinstance(mt, MessageTool) and mt._sent_in_turn:
            return None

        if is_heartbeat and not final_content.strip():
            return None

        compact_out = final_content.replace("\n", "\\n").replace("\t", "\\t")
        preview = compact_out[:120] + "..." if len(compact_out) > 120 else compact_out
        logger.info("Response session={} to {}:{}: {}", key, msg.channel, msg.sender_id, preview)
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},
            session_key=key,
        )

    def _clean_entry(self, m: dict, *, is_heartbeat: bool) -> dict | None:
        """Clean a raw message dict for session persistence.

        Returns the cleaned entry dict, or None if the entry should be skipped.
        Does NOT mutate the input dict.
        """
        from datetime import datetime

        entry = dict(m)
        role, content = entry.get("role"), entry.get("content")
        if role == "assistant" and not content and not entry.get("tool_calls"):
            return None  # skip empty assistant messages
        if is_heartbeat and (role != "assistant" or not content or entry.get("tool_calls")):
            return None
        if (
            role == "tool"
            and isinstance(content, str)
            and len(content) > self._TOOL_RESULT_MAX_CHARS
        ):
            entry["content"] = content[: self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
        elif role == "user":
            if isinstance(content, str):
                if content.startswith(ContextBuilder._RUNTIME_CONTEXT_TAG):
                    parts = content.split("\n\n", 1)
                    content = parts[1].strip() if len(parts) > 1 else ""
                mem_tag = ContextBuilder._MEMORY_CONTEXT_TAG
                if mem_tag in content:
                    content = content[: content.index(mem_tag)].strip()
                _BEGIN = "<BEGIN_USER_MESSAGE>"
                _END = "<END_USER_MESSAGE>"
                if _BEGIN in content and _END in content:
                    content = content[
                        content.index(_BEGIN) + len(_BEGIN) : content.index(_END)
                    ].strip()
                if not content:
                    return None
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
                    return None
                entry["content"] = filtered
        entry.setdefault("timestamp", datetime.now().isoformat())
        return entry

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

        # Throwaway subagent session — don't persist anything
        if session.key == "heartbeat:sub":
            return

        is_heartbeat = session.key == "heartbeat"
        saved_entries: list[dict[str, Any]] = []

        for m in messages[skip:]:
            entry = self._clean_entry(m, is_heartbeat=is_heartbeat)
            if entry is None:
                continue
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
        if self._subconscious and not session.key.startswith("heartbeat"):
            self._subconscious.feed_messages(messages[skip:], session_key=session.key)

    async def _consolidate_memory(
        self,
        session,
        archive_all: bool = False,
        *,
        generate_summary: bool = False,
    ) -> bool | str:
        """Consolidate session history: extract memories, summarize, then trim.

        When subconscious is active, runs a final extraction pass on the messages
        being compacted, generates a summary, inserts it as a system message in the
        session, then trims. Falls back to legacy MemoryStore when subconscious is
        disabled.

        Returns True/str on success (str is the summary when generate_summary=True),
        False on failure.
        """
        if self._subconscious:
            if archive_all:
                old_messages = session.messages
                keep_count = 0
            else:
                keep_count = self.memory_window // 2
                if len(session.messages) <= keep_count:
                    return True
                old_messages = (
                    session.messages[session.last_consolidated : -keep_count]
                    if keep_count
                    else session.messages[session.last_consolidated :]
                )
                if not old_messages:
                    return True

            logger.info(
                "Subconscious compaction: {} messages to compact, {} to keep",
                len(old_messages),
                keep_count,
            )
            self._subconscious._emit("consolidation", {"messages": len(old_messages), "kept": keep_count})

            # 1) Prune tool output before extraction/summarization
            pruned_messages = []
            for m in old_messages:
                if (
                    m.get("role") == "tool"
                    and isinstance(m.get("content", ""), str)
                    and len(m.get("content", "")) > 200
                ):
                    pruned_messages.append({**m, "content": m["content"][:200] + "\n[truncated]"})
                else:
                    pruned_messages.append(m)

            # 2) Extract memories from the messages being compacted
            extractable = [
                m
                for m in pruned_messages
                if m.get("role") in ("user", "assistant") and m.get("content")
            ]
            if extractable:
                try:
                    await self._subconscious._extract(extractable)
                    logger.info(
                        "Subconscious compaction: extracted memories from {} messages",
                        len(extractable),
                    )
                except Exception:
                    logger.exception(
                        "Subconscious compaction: extraction failed, continuing with summarization"
                    )

            # 3) Generate structured summary of compacted messages
            _SUMMARY_TEMPLATE = """Summarize this conversation using this exact structure:

### Goal
What the user is trying to accomplish

### Progress
What has been done so far

### Decisions
Key decisions made during this session

### Files Modified
List of files created/edited/deleted with brief description

### Next Steps
What remains to be done

### Critical Context
Important details that must not be lost"""

            summary = ""
            if generate_summary and extractable and self._subconscious._provider:
                conversation = "\n".join(f"[{m['role']}]: {m['content']}" for m in extractable)

                # Check for previous running summary for iterative update
                previous_summary = session.metadata.get("running_summary", "")

                if previous_summary:
                    user_content = (
                        f"Here is the previous conversation summary:\n{previous_summary}\n\n"
                        f"Here are new messages since that summary:\n{conversation}\n\n"
                        f"Update the summary to incorporate new information. Merge, don't duplicate.\n"
                        f"Use this structure:\n{_SUMMARY_TEMPLATE}"
                    )
                else:
                    user_content = f"{_SUMMARY_TEMPLATE}\n\n## Conversation\n{conversation}"

                try:
                    response = await self._subconscious._chat_with_fallback(
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Summarize this conversation using the provided structured template. "
                                    "Use [[Name]] wikilinks for people and entities."
                                ),
                            },
                            {"role": "user", "content": user_content},
                        ],
                        model=self._subconscious._config.extraction_model,
                    )
                    if response.is_error:
                        logger.error(
                            "Subconscious compaction: summarization LLM error: {}", response.content
                        )
                    elif response.content:
                        summary = response.content.strip()
                        # Persist running summary for iterative updates
                        session.metadata["running_summary"] = summary
                except Exception:
                    logger.exception("Subconscious compaction: summarization failed")

            # 3) Insert summary as system message in the session before the kept messages
            if summary:
                insert_pos = (
                    len(session.messages) - keep_count if keep_count else len(session.messages)
                )
                session.messages.insert(
                    insert_pos,
                    {
                        "role": "system",
                        "content": f"[Compaction Summary]\n\n{summary}",
                        "timestamp": datetime.now().isoformat(),
                        "compact_event": True,
                    },
                )
                logger.info("Subconscious compaction: summary inserted as system message")

            # 4) If summary was requested but failed, don't trim — we'd lose context
            #    without a summary to replace it. Return False so the caller knows.
            if generate_summary and not summary:
                logger.warning(
                    "Subconscious compaction: summary generation failed, skipping trim to preserve context"
                )
                return False

            # 5) Trim session — account for the inserted summary message
            if archive_all:
                extra = 1 if summary else 0
                session.last_consolidated = len(session.messages) - extra
            else:
                # keep_count messages + the summary message if inserted
                kept = keep_count + (1 if summary else 0)
                session.last_consolidated = len(session.messages) - kept

            compaction_meta = session.metadata.get("compaction")
            if not isinstance(compaction_meta, dict):
                compaction_meta = {}
            compaction_meta["count"] = self._to_int(compaction_meta.get("count")) + 1
            compaction_meta["last_compacted_at"] = datetime.now().isoformat()
            compaction_meta["last_compacted_messages"] = len(old_messages)
            compaction_meta["last_keep_count"] = keep_count
            compaction_meta["last_had_summary"] = bool(summary)
            session.metadata["compaction"] = compaction_meta

            return summary if summary else True

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
                result = await self._consolidate_memory(
                    session,
                    archive_all=archive_all,
                    generate_summary=True,
                )
                self.sessions.save(session)
                summary = result if isinstance(result, str) else ""
                return {
                    "ok": bool(result),
                    "archiveAll": archive_all,
                    "lastConsolidatedBefore": before,
                    "lastConsolidatedAfter": session.last_consolidated,
                    "messageCount": len(session.messages),
                    "historyEntry": summary,
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
        system_prompt: str | None = None,
    ) -> str:
        """Process a user message directly (for CLI/opencode usage)."""
        await self._connect_mcp()
        if self._subconscious and not self._subconscious._qmd.available:
            await self._subconscious.initialize()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(
            msg,
            session_key=session_key,
            on_progress=on_progress,
            model=model,
            system_prompt=system_prompt,
        )
        return response.content if response else ""

    async def process_system_direct(
        self,
        content: str,
        session_key: str = "main",
        channel: str = "cli",
        chat_id: str = "direct",
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """Process an internal system message inside an existing session."""
        await self._connect_mcp()
        if self._subconscious and not self._subconscious._qmd.available:
            await self._subconscious.initialize()

        session = self.sessions.get_or_create(session_key)
        active_model = model or self.model
        await self._refresh_model_limits(active_model)

        self._set_tool_context(channel, chat_id, None)
        history = session.get_history(max_messages=self.memory_window)
        stamped_history = self.context._stamp_history(history)
        initial_messages = [
            {"role": "system", "content": self.context.build_system_prompt()},
            *stamped_history,
            {"role": "system", "content": content},
        ]
        if system_prompt:
            initial_messages[0] = {"role": "system", "content": system_prompt}

        final_content, _, all_msgs, usage = await self._run_agent_loop(
            initial_messages,
            model=active_model,
        )
        self._save_turn(
            session,
            all_msgs,
            1 + len(stamped_history),
            usage=usage,
            model=active_model,
        )
        self.sessions.save(session)
        self._last_llm_usage[session_key] = usage
        return final_content or ""
