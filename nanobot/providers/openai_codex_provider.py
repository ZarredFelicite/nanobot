"""OpenAI Codex Responses Provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, AsyncGenerator

import httpx
from loguru import logger
from oauth_cli_kit import get_token as get_codex_token

from nanobot.providers.base import LLMProvider, LLMResponse, ModelLimits, ToolCallRequest

DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
DEFAULT_ORIGINATOR = "nanobot"
DEFAULT_CODEX_CLIENT_VERSION = "0.118.0"
MODELS_CACHE_TTL_S = 86400
CLIENT_VERSION_TTL_S = 86400


class OpenAICodexProvider(LLMProvider):
    """Use Codex OAuth to call the Responses API."""

    def __init__(self, default_model: str = "openai-codex/gpt-5.1-codex"):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model
        self._models_cache: dict[str, dict[str, Any]] = {}
        self._models_cache_ts = 0.0
        self._client_version = DEFAULT_CODEX_CLIENT_VERSION
        self._client_version_ts = 0.0
        self._models_lock = asyncio.Lock()
        self._client_version_lock = asyncio.Lock()

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
    ) -> LLMResponse:
        return await self.stream_chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            on_text_delta=None,
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
        model = model or self.default_model
        system_prompt, input_items = _convert_messages(messages)

        token = await asyncio.to_thread(get_codex_token)
        headers = _build_headers(token.account_id, token.access)

        body: dict[str, Any] = {
            "model": _strip_model_prefix(model),
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": _prompt_cache_key(messages),
            "tool_choice": "auto",
            "parallel_tool_calls": True,
        }

        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}

        if tools:
            body["tools"] = _convert_tools(tools)

        url = DEFAULT_CODEX_URL

        try:
            try:
                content, tool_calls, finish_reason, streamed_content = await _request_codex(
                    url, headers, body, verify=True, on_text_delta=on_text_delta
                )
            except Exception as e:
                if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                    raise
                logger.warning(
                    "SSL certificate verification failed for Codex API; retrying with verify=False"
                )
                content, tool_calls, finish_reason, streamed_content = await _request_codex(
                    url, headers, body, verify=False, on_text_delta=on_text_delta
                )
            return LLMResponse(
                content=content,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                streamed_content=streamed_content,
            )
        except Exception as e:
            return LLMResponse(
                content=f"Error calling Codex: {str(e)}",
                finish_reason="error",
            )

    async def get_model_limits(self, model: str | None = None) -> ModelLimits | None:
        model = model or self.default_model
        slug = _strip_model_prefix(model)
        catalog = await self._get_models_catalog()
        item = catalog.get(slug)
        if not item:
            return None

        context_tokens = item.get("context_window")
        max_output_tokens = None
        for key in (
            "max_output_tokens",
            "max_completion_tokens",
            "output_token_limit",
            "completion_token_limit",
        ):
            value = item.get(key)
            if isinstance(value, int) and value > 0:
                max_output_tokens = value
                break

        return ModelLimits(
            context_tokens=int(context_tokens) if isinstance(context_tokens, int) else None,
            max_output_tokens=max_output_tokens,
            metadata={
                "client_version": self._client_version,
                "slug": item.get("slug"),
                "display_name": item.get("display_name"),
                "truncation_policy": item.get("truncation_policy"),
                "minimal_client_version": item.get("minimal_client_version"),
            },
        )

    async def _get_models_catalog(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        if self._models_cache and (now - self._models_cache_ts) < MODELS_CACHE_TTL_S:
            return self._models_cache

        async with self._models_lock:
            now = time.time()
            if self._models_cache and (now - self._models_cache_ts) < MODELS_CACHE_TTL_S:
                return self._models_cache

            client_version = await self._get_client_version()
            token = await asyncio.to_thread(get_codex_token)
            headers = _build_headers(token.account_id, token.access)
            headers["accept"] = "application/json"
            headers["User-Agent"] = f"codex-cli/{client_version}"
            url = f"{DEFAULT_CODEX_MODELS_URL}?client_version={client_version}"

            try:
                async with httpx.AsyncClient(timeout=30.0, verify=True) as client:
                    response = await client.get(url, headers=headers)
                    response.raise_for_status()
                    data = response.json()
            except Exception:
                if client_version != DEFAULT_CODEX_CLIENT_VERSION:
                    logger.warning(
                        "Codex model catalog lookup failed for client_version {}; retrying with {}",
                        client_version,
                        DEFAULT_CODEX_CLIENT_VERSION,
                    )
                    fallback_url = (
                        f"{DEFAULT_CODEX_MODELS_URL}?client_version={DEFAULT_CODEX_CLIENT_VERSION}"
                    )
                    headers["User-Agent"] = f"codex-cli/{DEFAULT_CODEX_CLIENT_VERSION}"
                    async with httpx.AsyncClient(timeout=30.0, verify=True) as client:
                        response = await client.get(fallback_url, headers=headers)
                        response.raise_for_status()
                        data = response.json()
                    self._client_version = DEFAULT_CODEX_CLIENT_VERSION
                    self._client_version_ts = time.time()
                else:
                    raise

            models = data.get("models", []) if isinstance(data, dict) else []
            self._models_cache = {
                m.get("slug"): m for m in models if isinstance(m, dict) and isinstance(m.get("slug"), str)
            }
            self._models_cache_ts = time.time()
            return self._models_cache

    async def _get_client_version(self) -> str:
        now = time.time()
        if self._client_version and (now - self._client_version_ts) < CLIENT_VERSION_TTL_S:
            return self._client_version

        async with self._client_version_lock:
            now = time.time()
            if self._client_version and (now - self._client_version_ts) < CLIENT_VERSION_TTL_S:
                return self._client_version

            version = DEFAULT_CODEX_CLIENT_VERSION
            try:
                async with httpx.AsyncClient(timeout=10.0, verify=True) as client:
                    response = await client.get("https://registry.npmjs.org/@openai/codex/latest")
                    response.raise_for_status()
                    data = response.json()
                candidate = data.get("version") if isinstance(data, dict) else None
                if isinstance(candidate, str) and candidate.strip():
                    version = candidate.strip()
            except Exception:
                logger.debug("Failed to fetch latest Codex client version; using {}", version)

            self._client_version = version
            self._client_version_ts = time.time()
            return self._client_version

    def get_default_model(self) -> str:
        return self.default_model


def _strip_model_prefix(model: str) -> str:
    if model.startswith(("openai-codex/", "openai_codex/", "openai/")):
        return model.split("/", 1)[1]
    return model


def _build_headers(account_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": "nanobot (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


async def _request_codex(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    verify: bool,
    on_text_delta: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolCallRequest], str, bool]:
    async with httpx.AsyncClient(timeout=60.0, verify=verify) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            if response.status_code != 200:
                text = await response.aread()
                raise RuntimeError(
                    _friendly_error(response.status_code, text.decode("utf-8", "ignore"))
                )
            return await _consume_sse(response, on_text_delta=on_text_delta)


def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling schema to Codex flat format."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        fn = (tool.get("function") or {}) if tool.get("type") == "function" else tool
        name = fn.get("name")
        if not name:
            continue
        params = fn.get("parameters") or {}
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": fn.get("description") or "",
                "parameters": params if isinstance(params, dict) else {},
            }
        )
    return converted


def _convert_messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system_prompt = ""
    input_items: list[dict[str, Any]] = []

    for idx, msg in enumerate(messages):
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system_prompt = content if isinstance(content, str) else ""
            continue

        if role == "user":
            input_items.append(_convert_user_message(content))
            continue

        if role == "assistant":
            # Handle text first.
            if isinstance(content, str) and content:
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                        "status": "completed",
                        "id": f"msg_{idx}",
                    }
                )
            # Then handle tool calls.
            for tool_call in msg.get("tool_calls", []) or []:
                fn = tool_call.get("function") or {}
                call_id, item_id = _split_tool_call_id(tool_call.get("id"))
                call_id = call_id or f"call_{idx}"
                item_id = item_id or f"fc_{idx}"
                input_items.append(
                    {
                        "type": "function_call",
                        "id": item_id,
                        "call_id": call_id,
                        "name": fn.get("name"),
                        "arguments": fn.get("arguments") or "{}",
                    }
                )
            continue

        if role == "tool":
            call_id, _ = _split_tool_call_id(msg.get("tool_call_id"))
            output_text = (
                content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            )
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output_text,
                }
            )
            continue

    return system_prompt, input_items


def _convert_user_message(content: Any) -> dict[str, Any]:
    if isinstance(content, str):
        return {"role": "user", "content": [{"type": "input_text", "text": content}]}
    if isinstance(content, list):
        converted: list[dict[str, Any]] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                converted.append({"type": "input_text", "text": item.get("text", "")})
            elif item.get("type") == "image_url":
                url = (item.get("image_url") or {}).get("url")
                if url:
                    converted.append({"type": "input_image", "image_url": url, "detail": "auto"})
        if converted:
            return {"role": "user", "content": converted}
    return {"role": "user", "content": [{"type": "input_text", "text": ""}]}


def _split_tool_call_id(tool_call_id: Any) -> tuple[str, str | None]:
    if isinstance(tool_call_id, str) and tool_call_id:
        if "|" in tool_call_id:
            call_id, item_id = tool_call_id.split("|", 1)
            return call_id, item_id or None
        return tool_call_id, None
    return "call_0", None


def _prompt_cache_key(messages: list[dict[str, Any]]) -> str:
    raw = json.dumps(messages, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _iter_sse(response: httpx.Response) -> AsyncGenerator[dict[str, Any], None]:
    buffer: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if buffer:
                data_lines = [l[5:].strip() for l in buffer if l.startswith("data:")]
                buffer = []
                if not data_lines:
                    continue
                data = "\n".join(data_lines).strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    yield json.loads(data)
                except Exception:
                    continue
            continue
        buffer.append(line)


async def _consume_sse(
    response: httpx.Response,
    on_text_delta: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolCallRequest], str, bool]:
    content = ""
    tool_calls: list[ToolCallRequest] = []
    tool_call_buffers: dict[str, dict[str, Any]] = {}
    finish_reason = "stop"
    streamed_content = False

    async for event in _iter_sse(response):
        event_type = event.get("type")
        if event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                tool_call_buffers[call_id] = {
                    "id": item.get("id") or "fc_0",
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "",
                }
        elif event_type == "response.output_text.delta":
            delta = event.get("delta") or ""
            if not delta:
                continue
            content += delta
            if on_text_delta:
                await on_text_delta(delta)
                streamed_content = True
        elif event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id")
            if call_id and call_id in tool_call_buffers:
                tool_call_buffers[call_id]["arguments"] = event.get("arguments") or ""
        elif event_type == "response.output_item.done":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id")
                if not call_id:
                    continue
                buf = tool_call_buffers.get(call_id) or {}
                args_raw = buf.get("arguments") or item.get("arguments") or "{}"
                try:
                    args = json.loads(args_raw)
                except Exception:
                    args = {"raw": args_raw}
                tool_calls.append(
                    ToolCallRequest(
                        id=f"{call_id}|{buf.get('id') or item.get('id') or 'fc_0'}",
                        name=buf.get("name") or item.get("name"),
                        arguments=args,
                    )
                )
        elif event_type == "response.completed":
            status = (event.get("response") or {}).get("status")
            finish_reason = _map_finish_reason(status)
        elif event_type in {"error", "response.failed"}:
            raise RuntimeError("Codex response failed")

    return content, tool_calls, finish_reason, streamed_content


_FINISH_REASON_MAP = {
    "completed": "stop",
    "incomplete": "length",
    "failed": "error",
    "cancelled": "error",
}


def _map_finish_reason(status: str | None) -> str:
    return _FINISH_REASON_MAP.get(status or "completed", "stop")


def _friendly_error(status_code: int, raw: str) -> str:
    if status_code == 429:
        return "ChatGPT usage quota exceeded or rate limit triggered. Please try again later."
    return f"HTTP {status_code}: {raw}"
