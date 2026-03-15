"""Utilities for listing and probing configured models."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, cast

from nanobot.config.schema import Config

DEFAULT_EXACT_TEXT = "NANOBOT_MODEL_TEST_OK"
DEFAULT_TEST_TIMEOUT_S = 60.0
DEFAULT_PROBE_MAX_TOKENS = 256


@dataclass
class ConfiguredModel:
    """A configured model entry with source metadata."""

    model: str
    sources: list[str] = field(default_factory=list)
    provider_name: str | None = None
    auth_mode: str = "unknown"


@dataclass
class ModelProbeResult:
    """One model probe outcome."""

    model: str
    provider_name: str | None
    ttft_s: float | None = None
    total_s: float | None = None
    expected_text: str = DEFAULT_EXACT_TEXT
    actual_text: str = ""
    exact_match: bool = False
    error: str | None = None


def collect_configured_models(config: Config) -> list[ConfiguredModel]:
    """Return configured models in stable order with deduped targets."""
    ordered: dict[str, ConfiguredModel] = {}

    def _ensure(model: str, source: str) -> None:
        normalized = model.strip()
        if not normalized:
            return
        entry = ordered.get(normalized)
        if entry is None:
            provider_name = config.get_provider_name(normalized)
            entry = ConfiguredModel(
                model=normalized,
                sources=[source],
                provider_name=provider_name,
                auth_mode=_resolve_auth_mode(config, normalized, provider_name),
            )
            ordered[normalized] = entry
            return
        if source not in entry.sources:
            entry.sources.append(source)

    _ensure(config.agents.defaults.model, "default")

    primary = (config.models.primary or "").strip()
    if primary:
        _ensure(primary, "primary")

    for model in config.models.fallbacks:
        _ensure(model, "fallback")

    classifier_model = (config.tools.subconscious.classifier_model or "").strip()
    if classifier_model:
        _ensure(classifier_model, "subconscious-decision")

    decide_model = (config.gateway.heartbeat.decide_model or "").strip()
    if decide_model:
        _ensure(decide_model, "heartbeat-decision")

    return list(ordered.values())


async def probe_configured_models(
    config: Config,
    *,
    exact_text: str = DEFAULT_EXACT_TEXT,
    timeout_s: float = DEFAULT_TEST_TIMEOUT_S,
) -> list[ModelProbeResult]:
    """Probe each configured model sequentially for latency and exactness."""
    results: list[ModelProbeResult] = []
    for entry in collect_configured_models(config):
        result = await _probe_one_model(
            config, entry.model, exact_text=exact_text, timeout_s=timeout_s
        )
        results.append(result)
    return results


def _resolve_auth_mode(config: Config, model: str, provider_name: str | None) -> str:
    """Describe how a model authenticates from current config."""
    if provider_name is None:
        return "unresolved"

    from nanobot.providers.registry import find_by_name

    spec = find_by_name(provider_name)
    if spec and spec.is_oauth:
        return "oauth"

    provider = config.get_provider(model)
    if provider_name == "custom":
        if provider and provider.api_base:
            return "custom-api"
        return "custom-unset"
    if provider and provider.api_key:
        return "api-key"
    if config.get_api_base(model):
        return "api-base"
    return "missing"


def _make_provider_for_model(config: Config, model: str):
    """Create a provider instance for one specific model."""
    from nanobot.providers.custom_provider import CustomProvider
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider
    from nanobot.providers.registry import find_by_name

    provider_name = config.get_provider_name(model)
    if provider_name is None:
        raise RuntimeError(f"Could not determine provider for model '{model}'.")

    provider_config = config.get_provider(model)

    if provider_name == "openai_codex" or model.startswith("openai-codex/"):
        return OpenAICodexProvider(default_model=model)

    if provider_name == "custom":
        return CustomProvider(
            api_key=provider_config.api_key if provider_config else "no-key",
            api_base=config.get_api_base(model) or "http://localhost:8000/v1",
            default_model=model,
        )

    spec = find_by_name(provider_name)
    if (
        not model.startswith("bedrock/")
        and not (provider_config and provider_config.api_key)
        and not (spec and spec.is_oauth)
    ):
        raise RuntimeError(f"No credentials configured for model '{model}'.")

    return LiteLLMProvider(
        api_key=provider_config.api_key if provider_config else None,
        api_base=config.get_api_base(model),
        default_model=model,
        extra_headers=provider_config.extra_headers if provider_config else None,
        provider_name=provider_name,
    )


def _build_probe_messages(exact_text: str) -> list[dict[str, str]]:
    """Build a deterministic exact-match prompt."""
    return [
        {"role": "system", "content": "Reply with exactly the requested text and nothing else."},
        {
            "role": "user",
            "content": (
                "Return exactly the text inside <target> and nothing before or after.\n"
                f"<target>{exact_text}</target>"
            ),
        },
    ]


async def _probe_one_model(
    config: Config,
    model: str,
    *,
    exact_text: str,
    timeout_s: float,
) -> ModelProbeResult:
    """Probe one model and return timing plus exact-match data."""
    provider_name = config.get_provider_name(model)
    result = ModelProbeResult(model=model, provider_name=provider_name, expected_text=exact_text)

    try:
        provider = _make_provider_for_model(config, model)
        coro = _stream_probe(provider=provider, model=model, exact_text=exact_text)
        measured = await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        result.error = f"timed out after {timeout_s:.0f}s"
        return result
    except Exception as exc:
        result.error = str(exc)
        return result

    result.ttft_s = measured["ttft_s"]
    result.total_s = measured["total_s"]
    result.actual_text = measured["actual_text"]
    result.exact_match = result.actual_text.strip() == exact_text
    if not result.exact_match and not result.error:
        result.error = result.actual_text or "(empty response)"
    return result


async def _stream_probe(provider: Any, *, model: str, exact_text: str) -> dict[str, Any]:
    """Dispatch to the provider-specific stream probe."""
    from nanobot.providers.custom_provider import CustomProvider
    from nanobot.providers.litellm_provider import LiteLLMProvider
    from nanobot.providers.openai_codex_provider import OpenAICodexProvider

    if isinstance(provider, OpenAICodexProvider):
        return await _probe_codex(provider, model=model, exact_text=exact_text)
    if isinstance(provider, CustomProvider):
        return await _probe_custom(provider, model=model, exact_text=exact_text)
    if isinstance(provider, LiteLLMProvider):
        return await _probe_litellm(provider, model=model, exact_text=exact_text)
    raise RuntimeError(f"Unsupported provider type: {type(provider).__name__}")


async def _probe_litellm(provider: Any, *, model: str, exact_text: str) -> dict[str, Any]:
    """Measure TTFT and total time via LiteLLM streaming."""
    from litellm import acompletion

    resolved_model = provider._resolve_model(model)
    extra_msg_keys = provider._extra_msg_keys(model, resolved_model)
    request_messages = provider._sanitize_messages(
        provider._sanitize_empty_content(_build_probe_messages(exact_text)),
        extra_keys=extra_msg_keys,
    )

    kwargs: dict[str, Any] = {
        "model": resolved_model,
        "messages": request_messages,
        "max_tokens": max(DEFAULT_PROBE_MAX_TOKENS, len(exact_text) + 8),
        "temperature": 0,
        "stream": True,
    }
    provider._apply_model_overrides(resolved_model, kwargs)
    if provider.api_key:
        kwargs["api_key"] = provider.api_key
    if provider.api_base:
        kwargs["api_base"] = provider.api_base
    if provider.extra_headers:
        kwargs["extra_headers"] = provider.extra_headers

    started = time.perf_counter()
    stream = cast(Any, await acompletion(**kwargs))
    ttft_s: float | None = None
    parts: list[str] = []
    async for chunk in stream:
        delta = _extract_chunk_text(chunk)
        if not delta:
            continue
        if ttft_s is None:
            ttft_s = time.perf_counter() - started
        parts.append(delta)

    total_s = time.perf_counter() - started
    actual_text = "".join(parts).strip()
    if not actual_text:
        fallback = await provider.chat(
            messages=_build_probe_messages(exact_text),
            model=model,
            max_tokens=max(DEFAULT_PROBE_MAX_TOKENS, len(exact_text) + 8),
            temperature=0,
        )
        actual_text = (fallback.content or "").strip()
    return {"ttft_s": ttft_s, "total_s": total_s, "actual_text": actual_text}


async def _probe_custom(provider: Any, *, model: str, exact_text: str) -> dict[str, Any]:
    """Measure TTFT and total time via direct OpenAI-compatible streaming."""
    started = time.perf_counter()
    stream: Any = await provider._client.chat.completions.create(
        model=model,
        messages=provider._sanitize_empty_content(_build_probe_messages(exact_text)),
        max_tokens=max(DEFAULT_PROBE_MAX_TOKENS, len(exact_text) + 8),
        temperature=0,
        stream=True,
    )

    ttft_s: float | None = None
    parts: list[str] = []
    async for chunk in stream:
        delta = _extract_chunk_text(chunk)
        if not delta:
            continue
        if ttft_s is None:
            ttft_s = time.perf_counter() - started
        parts.append(delta)

    total_s = time.perf_counter() - started
    actual_text = "".join(parts).strip()
    if not actual_text:
        fallback = await provider.chat(
            messages=_build_probe_messages(exact_text),
            model=model,
            max_tokens=max(DEFAULT_PROBE_MAX_TOKENS, len(exact_text) + 8),
            temperature=0,
        )
        actual_text = (fallback.content or "").strip()
    return {"ttft_s": ttft_s, "total_s": total_s, "actual_text": actual_text}


async def _probe_codex(provider: Any, *, model: str, exact_text: str) -> dict[str, Any]:
    """Measure TTFT and total time against the Codex SSE API."""
    import httpx

    from oauth_cli_kit import get_token as get_codex_token

    from nanobot.providers.openai_codex_provider import (
        DEFAULT_CODEX_URL,
        _build_headers,
        _convert_messages,
        _iter_sse,
        _prompt_cache_key,
        _strip_model_prefix,
    )

    messages = _build_probe_messages(exact_text)
    system_prompt, input_items = _convert_messages(messages)
    token = await asyncio.to_thread(get_codex_token)
    if not token.account_id or not token.access:
        raise RuntimeError("OpenAI Codex OAuth token is unavailable.")
    account_id = token.account_id
    access_token = token.access
    headers = _build_headers(account_id, access_token)
    body: dict[str, Any] = {
        "model": _strip_model_prefix(model),
        "store": False,
        "stream": True,
        "instructions": system_prompt,
        "input": input_items,
        "text": {"verbosity": "low"},
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": _prompt_cache_key(messages),
    }

    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=60.0, verify=True) as client:
        async with client.stream("POST", DEFAULT_CODEX_URL, headers=headers, json=body) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise RuntimeError(text.decode("utf-8", "ignore"))

            ttft_s: float | None = None
            parts: list[str] = []
            async for event in _iter_sse(response):
                if event.get("type") != "response.output_text.delta":
                    continue
                delta = event.get("delta") or ""
                if not delta:
                    continue
                if ttft_s is None:
                    ttft_s = time.perf_counter() - started
                parts.append(delta)

    total_s = time.perf_counter() - started
    actual_text = "".join(parts).strip()
    if not actual_text:
        fallback = await provider.chat(
            messages=messages,
            model=model,
            max_tokens=max(DEFAULT_PROBE_MAX_TOKENS, len(exact_text) + 8),
            temperature=0,
        )
        actual_text = (fallback.content or "").strip()
    return {"ttft_s": ttft_s, "total_s": total_s, "actual_text": actual_text}


def _extract_chunk_text(chunk: Any) -> str:
    """Best-effort text extraction from streamed OpenAI/LiteLLM chunks."""
    choices = getattr(chunk, "choices", None)
    if choices is None and isinstance(chunk, dict):
        choices = chunk.get("choices")
    if not choices:
        return ""

    first = choices[0]
    delta = getattr(first, "delta", None)
    if delta is None and isinstance(first, dict):
        delta = first.get("delta")

    content = getattr(delta, "content", None)
    if content is None and isinstance(delta, dict):
        content = delta.get("content")
    if content is None:
        message = getattr(first, "message", None)
        content = getattr(message, "content", None)
    if content is None and isinstance(first, dict):
        message = first.get("message")
        if isinstance(message, dict):
            content = message.get("content")
    if content is None:
        content = getattr(first, "text", None)
    if content is None and isinstance(first, dict):
        content = first.get("text")

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
        return "".join(parts)
    return ""
