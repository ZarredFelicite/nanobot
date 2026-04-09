"""LiteLLM provider implementation for multi-provider support."""

import os
import secrets
import string
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

import json_repair
import litellm
from litellm import acompletion
from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse, ModelLimits, ToolCallRequest
from nanobot.providers.registry import find_by_model, find_gateway

# Standard chat-completion message keys.
_ALLOWED_MSG_KEYS = frozenset(
    {"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"}
)
_ANTHROPIC_EXTRA_KEYS = frozenset({"thinking_blocks"})
_ALNUM = string.ascii_letters + string.digits
_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_MODELS_TTL_S = 86400


def _short_tool_id() -> str:
    """Generate a 9-char alphanumeric ID compatible with all providers (incl. Mistral)."""
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}

        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        self._gateway = find_gateway(provider_name, api_key, api_base)

        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)

        if api_base:
            litellm.api_base = api_base

        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
        self._model_limits_cache: dict[str, dict[str, Any]] = {}
        self._model_limits_cache_ts = 0.0

    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            return
        if not spec.env_key:
            # OAuth/provider-only specs (for example: openai_codex)
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    def _resolve_model(self, model: str) -> str:
        """Resolve model name by applying provider/gateway prefixes."""
        if self._gateway:
            # Gateway mode: apply gateway prefix, skip provider-specific prefixes
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                model = model.split("/")[-1]
            if prefix and not model.startswith(f"{prefix}/"):
                model = f"{prefix}/{model}"
            return model

        # Standard mode: auto-prefix for known providers
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            model = self._canonicalize_explicit_prefix(model, spec.name, spec.litellm_prefix)
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"

        return model

    @staticmethod
    def _canonicalize_explicit_prefix(model: str, spec_name: str, canonical_prefix: str) -> str:
        """Normalize explicit provider prefixes like `github-copilot/...`."""
        if "/" not in model:
            return model
        prefix, remainder = model.split("/", 1)
        if prefix.lower().replace("-", "_") != spec_name:
            return model
        return f"{canonical_prefix}/{remainder}"

    def _supports_cache_control(self, model: str) -> bool:
        """Return True when the provider supports cache_control on content blocks."""
        if self._gateway is not None:
            return self._gateway.supports_prompt_caching
        spec = find_by_model(model)
        return spec is not None and spec.supports_prompt_caching

    def _apply_cache_control(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Return copies of messages and tools with cache_control injected."""
        new_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                content = msg["content"]
                if isinstance(content, str):
                    new_content = [
                        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                    ]
                else:
                    new_content = list(content)
                    new_content[-1] = {**new_content[-1], "cache_control": {"type": "ephemeral"}}
                new_messages.append({**msg, "content": new_content})
            else:
                new_messages.append(msg)

        new_tools = tools
        if tools:
            new_tools = list(tools)
            new_tools[-1] = {**new_tools[-1], "cache_control": {"type": "ephemeral"}}

        return new_messages, new_tools

    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        model_lower = model.lower()
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    return

    @staticmethod
    def _extra_msg_keys(original_model: str, resolved_model: str) -> frozenset[str]:
        """Return provider-specific extra keys to preserve in request messages."""
        spec = find_by_model(original_model) or find_by_model(resolved_model)
        if (
            (spec and spec.name == "anthropic")
            or "claude" in original_model.lower()
            or resolved_model.startswith("anthropic/")
        ):
            return _ANTHROPIC_EXTRA_KEYS
        return frozenset()

    @staticmethod
    def _sanitize_messages(
        messages: list[dict[str, Any]], extra_keys: frozenset[str] = frozenset()
    ) -> list[dict[str, Any]]:
        """Strip non-standard keys and ensure assistant messages have a content key."""
        max_tool_call_id_length = 64
        allowed = _ALLOWED_MSG_KEYS | extra_keys
        sanitized = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in allowed}
            # Strict providers require "content" even when assistant only has tool_calls
            if clean.get("role") == "assistant" and "content" not in clean:
                clean["content"] = None
            if "tool_call_id" in clean and clean["tool_call_id"]:
                tool_call_id = clean["tool_call_id"]
                if isinstance(tool_call_id, str) and len(tool_call_id) > max_tool_call_id_length:
                    clean["tool_call_id"] = tool_call_id[:32] + tool_call_id[-32:]
            sanitized.append(clean)
        return sanitized

    @staticmethod
    def _normalize_stepfun_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize assistant tool-call turns for stricter StepFun parsers.

        StepFun can reject assistant messages that include both non-null content
        and tool_calls. Force content to null for those turns.
        """
        normalized: list[dict[str, Any]] = []
        for msg in messages:
            clean = dict(msg)
            # Provider appears strict about non-standard assistant metadata.
            clean.pop("reasoning_content", None)
            clean.pop("thinking_blocks", None)

            if clean.get("role") == "assistant" and clean.get("tool_calls"):
                # StepFun docs/examples use empty string for assistant tool-call
                # turns; null can be rejected by stricter parsers.
                clean["content"] = ""
            if clean.get("role") == "tool":
                # OpenRouter docs mark name optional; keep the minimal shape.
                clean.pop("name", None)
            normalized.append(clean)
        return normalized

    async def get_model_limits(self, model: str | None = None) -> ModelLimits | None:
        original_model = model or self.default_model
        if not original_model.startswith("openrouter/"):
            return None

        lookup_id = original_model.split("/", 1)[1]
        catalog = await self._get_openrouter_models_catalog()
        item = catalog.get(lookup_id)
        if not item:
            return None

        context_tokens = item.get("context_length")
        return ModelLimits(
            context_tokens=int(context_tokens) if isinstance(context_tokens, int) else None,
            max_output_tokens=None,
            metadata={
                "id": item.get("id"),
                "name": item.get("name"),
                "top_provider": item.get("top_provider"),
            },
        )

    async def _get_openrouter_models_catalog(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        if self._model_limits_cache and (now - self._model_limits_cache_ts) < _OPENROUTER_MODELS_TTL_S:
            return self._model_limits_cache

        headers = {"accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        async with httpx.AsyncClient(timeout=30.0, verify=True) as client:
            response = await client.get(_OPENROUTER_MODELS_URL, headers=headers)
            response.raise_for_status()
            data = response.json()

        models = data.get("data", []) if isinstance(data, dict) else []
        self._model_limits_cache = {
            m.get("id"): m for m in models if isinstance(m, dict) and isinstance(m.get("id"), str)
        }
        self._model_limits_cache_ts = time.time()
        return self._model_limits_cache

    def _build_request_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
        reasoning_effort: str | None,
    ) -> dict[str, Any]:
        original_model = model or self.default_model
        resolved_model = self._resolve_model(original_model)
        extra_msg_keys = self._extra_msg_keys(original_model, resolved_model)

        if self._supports_cache_control(original_model):
            messages, tools = self._apply_cache_control(messages, tools)

        max_tokens = max(1, max_tokens)
        request_messages = self._sanitize_messages(
            self._sanitize_empty_content(messages), extra_keys=extra_msg_keys
        )

        if resolved_model.startswith("openrouter/stepfun/"):
            request_messages = self._normalize_stepfun_tool_messages(request_messages)

        kwargs: dict[str, Any] = {
            "model": resolved_model,
            "messages": request_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        self._apply_model_overrides(resolved_model, kwargs)

        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
            kwargs["drop_params"] = True
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        """Send a chat completion request via LiteLLM."""
        kwargs = self._build_request_kwargs(
            messages, tools, model, max_tokens, temperature, reasoning_effort
        )
        try:
            response = await acompletion(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )

    async def stream_chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        if on_text_delta is None:
            return await self.chat(
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
            )

        kwargs = self._build_request_kwargs(
            messages, tools, model, max_tokens, temperature, reasoning_effort
        )
        kwargs["stream"] = True

        try:
            stream = await acompletion(**kwargs)
            return await self._parse_stream(
                stream,
                fallback_kwargs={k: v for k, v in kwargs.items() if k != "stream"},
                on_text_delta=on_text_delta,
                on_reasoning_delta=on_reasoning_delta,
            )
        except Exception as e:
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )

    def _coerce_tool_calls(self, raw_tool_calls: list[Any]) -> list[ToolCallRequest]:
        tool_calls = []
        for tc in raw_tool_calls:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            args = getattr(fn, "arguments", {})
            if isinstance(args, str):
                try:
                    args = json_repair.loads(args)
                except Exception:
                    args = {}
            parsed_args: dict[str, Any]
            if isinstance(args, dict):
                parsed_args = args
            else:
                parsed_args = {}
            tool_calls.append(
                ToolCallRequest(
                    id=getattr(tc, "id", None) or _short_tool_id(),
                    name=getattr(fn, "name", None),
                    arguments=parsed_args,
                )
            )
        return tool_calls

    @staticmethod
    def _delta_attr(obj: Any, key: str) -> Any:
        if obj is None:
            return None
        value = getattr(obj, key, None)
        if value is None and isinstance(obj, dict):
            value = obj.get(key)
        return value

    @classmethod
    def _extract_delta_text(cls, delta: Any) -> str:
        content = cls._delta_attr(delta, "content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                else:
                    text = getattr(item, "text", None)
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)
        text = cls._delta_attr(delta, "text")
        return text if isinstance(text, str) else ""

    @classmethod
    def _extract_delta_reasoning(cls, delta: Any) -> str:
        for key in ("reasoning_content", "reasoning", "reasoning_text"):
            value = cls._delta_attr(delta, key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                parts: list[str] = []
                for item in value:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict):
                        text = item.get("text") or item.get("content")
                        if isinstance(text, str):
                            parts.append(text)
                if parts:
                    return "".join(parts)
        return ""

    async def _parse_stream(
        self,
        stream: Any,
        *,
        fallback_kwargs: dict[str, Any],
        on_text_delta: Callable[[str], Awaitable[None]],
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None,
    ) -> LLMResponse:
        parts: list[str] = []
        reasoning_parts: list[str] = []
        raw_tool_calls: list[Any] = []
        finish_reason = "stop"
        usage: dict[str, int] = {}
        streamed_content = False

        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            for choice in choices:
                delta = getattr(choice, "delta", None)
                reasoning = self._extract_delta_reasoning(delta)
                if reasoning:
                    reasoning_parts.append(reasoning)
                    if on_reasoning_delta:
                        await on_reasoning_delta(reasoning)
                text = self._extract_delta_text(delta)
                if text:
                    parts.append(text)
                    await on_text_delta(text)
                    streamed_content = True
                delta_tool_calls = self._delta_attr(delta, "tool_calls")
                if delta_tool_calls:
                    raw_tool_calls = list(delta_tool_calls)
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage:
                usage = {
                    "prompt_tokens": getattr(chunk_usage, "prompt_tokens", 0),
                    "completion_tokens": getattr(chunk_usage, "completion_tokens", 0),
                    "total_tokens": getattr(chunk_usage, "total_tokens", 0),
                }

        content = "".join(parts) or None
        tool_calls = self._coerce_tool_calls(raw_tool_calls)
        reasoning_content = "".join(reasoning_parts) or None

        if not content and not tool_calls and not reasoning_content:
            fallback = await acompletion(**fallback_kwargs)
            return self._parse_response(fallback)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
            streamed_content=streamed_content,
        )

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message
        content = message.content
        finish_reason = choice.finish_reason

        raw_tool_calls = []
        for ch in response.choices:
            msg = ch.message
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                raw_tool_calls.extend(msg.tool_calls)
                if ch.finish_reason in ("tool_calls", "stop"):
                    finish_reason = ch.finish_reason
            if not content and msg.content:
                content = msg.content

        if len(response.choices) > 1:
            logger.debug(
                "LiteLLM response has {} choices, merged {} tool_calls",
                len(response.choices),
                len(raw_tool_calls),
            )

        tool_calls = self._coerce_tool_calls(raw_tool_calls)

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        reasoning_content = getattr(message, "reasoning_content", None) or None
        thinking_blocks = getattr(message, "thinking_blocks", None) or None

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        )

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
