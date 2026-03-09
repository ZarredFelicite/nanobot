"""OpenCode TUI HTTP+SSE channel.

Implements the HTTP REST + Server-Sent Events API that the OpenCode TUI
(https://github.com/anomalyco/opencode) expects, allowing it to connect
to nanobot as its backend.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.channels.base import BaseChannel

if TYPE_CHECKING:
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus
    from nanobot.config.schema import AgentDefaults, ModelsConfig, OpenCodeConfig, PermissionConfig
    from nanobot.session.manager import Session, SessionManager


class OpenCodeChannel(BaseChannel):
    """HTTP+SSE channel compatible with the OpenCode TUI."""

    name = "opencode"

    def __init__(
        self,
        config: OpenCodeConfig,
        bus: MessageBus,
        session_manager: SessionManager | None = None,
        agent_loop: AgentLoop | None = None,
        agent_config: AgentDefaults | None = None,
        models_config: ModelsConfig | None = None,
        permission_config: PermissionConfig | None = None,
    ):
        super().__init__(config, bus)
        self.session_manager = session_manager
        self.agent_loop = agent_loop
        self.agent_config = agent_config
        self.models_config = models_config
        self.permission_config = permission_config
        self.port = config.port

        self._sse_clients: list[web.StreamResponse] = []
        self._id_counter = 0
        self._active_tasks: dict[str, asyncio.Task] = {}
        self._pending_permissions: dict[str, asyncio.Future] = {}
        self._last_context_by_session: dict[str, dict[str, Any]] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------------
    # ID generation
    # ------------------------------------------------------------------

    def _next_id(self, prefix: str = "msg") -> str:
        self._id_counter += 1
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _epoch_ms(ts: float) -> int:
        return int(ts * 1000)

    @staticmethod
    def _default_title_for_session(session: Session) -> str:
        return f"session-{session.created_at.strftime('%Y%m%d-%H%M%S')}"

    def _new_session_id(self) -> str:
        return f"session-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')[:22]}"

    def _session_exists(self, key: str) -> bool:
        if not self.session_manager:
            return False
        if key in self.session_manager._cache:
            return True
        return any(s.get("key") == key for s in self.session_manager.list_sessions())

    def _split_model(self, full_model: str) -> tuple[str, str]:
        """Split full model name into (provider, short model id)."""
        if "/" in full_model:
            prefix, short = full_model.split("/", 1)
            return prefix, short

        provider = self.agent_config.provider if self.agent_config else "nanobot"

        if provider == "auto":
            provider = "nanobot"
        return provider, full_model

    def _configured_model_names(self) -> list[str]:
        """Collect configured model names in priority order."""
        ordered: list[str] = []

        primary = (self.models_config.primary if self.models_config else "").strip()
        if primary:
            ordered.append(primary)

        if self.models_config:
            for model in self.models_config.fallbacks:
                if isinstance(model, str) and model.strip() and model not in ordered:
                    ordered.append(model)

        if (
            not ordered
            and self.agent_config
            and self.agent_config.model
            and self.agent_config.model not in ordered
        ):
            ordered.append(self.agent_config.model)

        if not ordered:
            ordered.append("default")

        return ordered

    def _build_provider_entry(self, provider_name: str) -> dict[str, Any]:
        return {
            "id": provider_name,
            "name": provider_name.title(),
            "source": "env",
            "env": [],
            "options": {},
            "models": {},
        }

    def _model_catalog(self) -> tuple[list[dict[str, Any]], str]:
        """Build OpenCode provider catalog and return (providers, default model)."""
        provider_entries: dict[str, dict[str, Any]] = {}
        ordered_provider_names: list[str] = []
        model_names = self._configured_model_names()

        for full_model in model_names:
            provider_name, model_id = self._split_model(full_model)

            if provider_name not in provider_entries:
                provider_entries[provider_name] = self._build_provider_entry(provider_name)
                ordered_provider_names.append(provider_name)

            provider_entries[provider_name]["models"][model_id] = {
                "id": model_id,
                "providerID": provider_name,
                "name": model_id,
                "api": {"id": "anthropic", "url": "", "npm": ""},
                "capabilities": {
                    "temperature": True,
                    "reasoning": False,
                    "attachment": False,
                    "toolcall": True,
                    "input": {
                        "text": True,
                        "audio": False,
                        "image": True,
                        "video": False,
                        "pdf": False,
                    },
                    "output": {
                        "text": True,
                        "audio": False,
                        "image": False,
                        "video": False,
                        "pdf": False,
                    },
                },
                "cost": {"input": 0, "output": 0, "cache": {"read": 0, "write": 0}},
                "limit": {"context": 200000, "output": 8192},
                "status": "active",
                "options": {},
                "headers": {},
            }

        providers = [provider_entries[name] for name in ordered_provider_names]
        default_model = model_names[0]
        return providers, default_model

    def _parse_model(self) -> tuple[str, str]:
        """Return (provider_name, short_model_id) from agent config.

        Nanobot stores models as "provider/model-name" (e.g. "anthropic/claude-opus-4-5").
        OpenCode expects the provider ID and model ID to be separate, with the models
        dict keyed by the short model ID (without provider prefix).
        """
        full_model = self._configured_model_names()[0]
        return self._split_model(full_model)

    def _extract_requested_model(self, body: dict[str, Any] | None) -> str | None:
        """Extract full model name from OpenCode request payload if provided."""
        if not isinstance(body, dict):
            return None

        model_value = body.get("model")
        if isinstance(model_value, str) and model_value.strip():
            return model_value.strip()

        if isinstance(model_value, dict):
            provider_id = model_value.get("providerID")
            model_id = model_value.get("modelID")
            if (
                isinstance(provider_id, str)
                and isinstance(model_id, str)
                and provider_id
                and model_id
            ):
                return f"{provider_id}/{model_id}"

        provider_id = body.get("providerID")
        model_id = body.get("modelID")
        if isinstance(provider_id, str) and isinstance(model_id, str) and provider_id and model_id:
            return f"{provider_id}/{model_id}"

        return None

    def _session_model(self, session: Session, body: dict[str, Any] | None = None) -> str:
        requested = self._extract_requested_model(body)
        if requested:
            session.metadata["model"] = requested
            return requested

        stored = session.metadata.get("model")
        if isinstance(stored, str) and stored.strip():
            return stored.strip()

        return self._configured_model_names()[0]

    # ------------------------------------------------------------------
    # BaseChannel interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._app = web.Application()
        self._register_routes(self._app)

        # Wire up permission callback on the agent loop
        if self.agent_loop:
            self.agent_loop._permission_callback = self._permission_callback
            if self.permission_config:
                self.agent_loop._require_approval = list(self.permission_config.require_approval)

        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        logger.info("OpenCode API listening on http://127.0.0.1:{}", self.port)

        # Keep alive
        while self._running:
            await asyncio.sleep(1)

    async def stop(self) -> None:
        self._running = False
        # Close SSE clients
        for client in list(self._sse_clients):
            try:
                await client.write_eof()
            except Exception:
                pass
        self._sse_clients.clear()

        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    async def send(self, msg: OutboundMessage) -> None:
        # Outbound messages from the bus are not directly used by OpenCode;
        # the channel pushes SSE events during message processing instead.
        pass

    # ------------------------------------------------------------------
    # Route registration
    # ------------------------------------------------------------------

    def _register_routes(self, app: web.Application) -> None:
        # Bootstrap endpoints (TUI blocks on all 4)
        app.router.add_get("/config/providers", self._handle_config_providers)
        app.router.add_get("/provider", self._handle_provider)
        app.router.add_get("/agent", self._handle_agent)
        app.router.add_get("/config", self._handle_config)

        # SSE
        app.router.add_get("/event", self._handle_sse)
        app.router.add_get("/global/event", self._handle_global_sse)

        # Sessions
        app.router.add_post("/session", self._handle_session_create)
        app.router.add_get("/session", self._handle_session_list)
        app.router.add_get("/session/status", self._handle_session_status)
        app.router.add_get("/session/{id}", self._handle_session_get)
        app.router.add_patch("/session/{id}", self._handle_session_patch)
        app.router.add_delete("/session/{id}", self._handle_session_delete)

        # Messages
        app.router.add_get("/session/{id}/message", self._handle_session_messages)
        app.router.add_post("/session/{id}/message", self._handle_session_send)
        app.router.add_delete(
            "/session/{id}/message/{messageId}", self._handle_message_revert
        )
        app.router.add_post(
            "/session/{id}/message/{messageId}/unrevert", self._handle_message_unrevert
        )
        app.router.add_post("/session/{id}/prompt_async", self._handle_prompt_async)
        app.router.add_post("/session/{id}/abort", self._handle_session_abort)
        app.router.add_post(
            "/session/{id}/permissions/{permissionId}", self._handle_permission_reply
        )
        app.router.add_post("/session/{id}/init", self._handle_session_init)
        app.router.add_post("/session/{id}/fork", self._handle_session_fork)
        app.router.add_post("/session/{id}/command", self._handle_session_command)
        app.router.add_post("/session/{id}/summarize", self._handle_session_summarize)
        app.router.add_get("/session/{id}/children", self._handle_stub_list)
        app.router.add_get("/session/{id}/todo", self._handle_stub_list)
        app.router.add_get("/session/{id}/diff", self._handle_stub_list)

        # Stubs
        app.router.add_get("/command", self._handle_command_list)
        app.router.add_get("/skill", self._handle_stub_list)
        app.router.add_get("/lsp", self._handle_stub_dict)
        app.router.add_get("/mcp", self._handle_stub_dict)
        app.router.add_get("/experimental/resource", self._handle_stub_list)
        app.router.add_get("/formatter", self._handle_stub_dict)
        app.router.add_get("/provider/auth", self._handle_stub_dict)
        app.router.add_get("/vcs", self._handle_vcs)
        app.router.add_get("/path", self._handle_path)
        app.router.add_get("/find", self._handle_stub_list)
        app.router.add_get("/find/file", self._handle_stub_list)
        app.router.add_get("/find/symbol", self._handle_stub_list)
        app.router.add_get("/file", self._handle_stub_list)
        app.router.add_get("/file/content", self._handle_stub_dict)
        app.router.add_get("/file/status", self._handle_stub_list)
        app.router.add_get("/global/health", self._handle_health)
        app.router.add_get("/global/config", self._handle_config)
        app.router.add_get("/permission", self._handle_stub_list)
        app.router.add_get("/question", self._handle_stub_list)
        app.router.add_post("/log", self._handle_stub_ok)
        app.router.add_post("/instance/dispose", self._handle_stub_ok)
        app.router.add_post("/global/dispose", self._handle_stub_ok)

    # ------------------------------------------------------------------
    # Bootstrap endpoints
    # ------------------------------------------------------------------

    async def _handle_config_providers(self, request: web.Request) -> web.Response:
        providers, default_model = self._model_catalog()

        return web.json_response(
            {
                "providers": providers,
                "default": {"default": default_model},
            }
        )

    async def _handle_provider(self, request: web.Request) -> web.Response:
        providers, _ = self._model_catalog()
        return web.json_response(providers)

    async def _handle_agent(self, request: web.Request) -> web.Response:
        return web.json_response(
            [
                {
                    "name": "default",
                    "description": "nanobot agent",
                    "mode": "primary",
                    "builtIn": True,
                    "permission": {"edit": True, "bash": True},
                    "tools": {},
                    "options": {},
                }
            ]
        )

    async def _handle_config(self, request: web.Request) -> web.Response:
        providers, default_model = self._model_catalog()
        provider_map = {
            provider["id"]: {
                "models": {model_id: {} for model_id in provider["models"]},
            }
            for provider in providers
        }

        return web.json_response(
            {
                "theme": "catppuccin-mocha",
                "keybinds": {},
                "tui": {},
                "model": default_model,
                "provider": provider_map,
                "mcp": {},
                "agent": {},
                "permission": {},
                "tools": {},
            }
        )

    # ------------------------------------------------------------------
    # SSE endpoint
    # ------------------------------------------------------------------

    async def _handle_sse(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        resp.headers["Cache-Control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        await resp.prepare(request)

        self._sse_clients.append(resp)

        # Send initial connected event
        await self._sse_write(resp, "server.connected", {})

        try:
            while resp.task is not None and not resp.task.done():
                await self._sse_write(resp, "server.heartbeat", {})
                await asyncio.sleep(10)
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            if resp in self._sse_clients:
                self._sse_clients.remove(resp)

        return resp

    async def _handle_global_sse(self, request: web.Request) -> web.StreamResponse:
        # Reuse the same handler — the TUI just needs a stream
        return await self._handle_sse(request)

    async def _sse_write(self, resp: web.StreamResponse, event_type: str, properties: dict) -> None:
        payload = json.dumps({"type": event_type, "properties": properties})
        try:
            await resp.write(f"data: {payload}\n\n".encode())
        except (ConnectionResetError, ConnectionAbortedError, RuntimeError):
            if resp in self._sse_clients:
                self._sse_clients.remove(resp)

    async def _broadcast_sse(self, event_type: str, properties: dict) -> None:
        for client in list(self._sse_clients):
            await self._sse_write(client, event_type, properties)

    # ------------------------------------------------------------------
    # Session endpoints
    # ------------------------------------------------------------------

    async def _handle_session_create(self, request: web.Request) -> web.Response:
        if not self.session_manager:
            return web.json_response({"error": "no session manager"}, status=500)

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        requested_id = body.get("id")
        is_empty_body = not body

        if requested_id == "main":
            session_id = "main"
        elif is_empty_body and not self._session_exists("main"):
            session_id = "main"
        else:
            session_id = self._new_session_id()
        key = session_id
        session = self.session_manager.get_or_create(key)

        requested_title = body.get("title")
        if isinstance(requested_title, str) and requested_title.strip():
            session.metadata["title"] = requested_title.strip()
        elif "title" not in session.metadata:
            session.metadata["title"] = self._default_title_for_session(session)

        self.session_manager.save(session)

        info = self._session_to_info(session, session_id)
        await self._broadcast_sse("session.created", {"info": info})
        return web.json_response(info)

    async def _handle_session_list(self, request: web.Request) -> web.Response:
        if not self.session_manager:
            return web.json_response([])

        sessions = self.session_manager.list_sessions()
        result = []
        for s in sessions:
            key = s.get("key", "")
            # Include all sessions, derive id from key
            sid = key.split(":", 1)[1] if ":" in key else key
            session = self.session_manager.get_or_create(key)
            result.append(self._session_to_info(session, sid))

        return web.json_response(result)

    async def _handle_session_get(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        session, key = self._find_session(session_id)
        if not session:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(self._session_to_info(session, session_id))

    async def _handle_session_patch(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        session, key = self._find_session(session_id)
        if not session:
            return web.json_response({"error": "not found"}, status=404)

        body = await request.json()
        if "title" in body:
            session.metadata["title"] = body["title"]
        requested_model = self._extract_requested_model(body)
        if requested_model:
            session.metadata["model"] = requested_model
        self.session_manager.save(session)

        info = self._session_to_info(session, session_id)
        await self._broadcast_sse("session.updated", {"info": info})
        return web.json_response(info)

    async def _handle_session_delete(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        session, key = self._find_session(session_id)
        if not session:
            return web.json_response({"error": "not found"}, status=404)

        # Delete session file
        path = self.session_manager._get_session_path(key)
        if path.exists():
            path.unlink()
        self.session_manager.invalidate(key)

        info = self._session_to_info(session, session_id)
        await self._broadcast_sse("session.deleted", {"info": info})
        return web.json_response(info)

    async def _handle_session_status(self, request: web.Request) -> web.Response:
        session_id = request.query.get("sessionID") or request.query.get("id")

        def _status_payload(sid: str) -> dict[str, Any]:
            context = self._last_context_by_session.get(sid, {})
            return {
                "sessionID": sid,
                "status": {"type": "idle", "context": context},
            }

        if session_id:
            return web.json_response(_status_payload(session_id))

        sessions: list[str] = []
        if self.session_manager:
            sessions = [
                s.get("key", "") for s in self.session_manager.list_sessions() if s.get("key")
            ]

        known = {sid.split(":", 1)[1] if ":" in sid else sid for sid in sessions}
        known.update(self._last_context_by_session.keys())
        return web.json_response([_status_payload(sid) for sid in sorted(known)])

    async def _handle_session_init(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _handle_session_fork(self, request: web.Request) -> web.Response:
        """Fork a session — copy its history to a new session."""
        session_id = request.match_info["id"]
        session, key = self._find_session(session_id)
        if not session:
            return web.json_response({"error": "session not found"}, status=404)
        if not self.session_manager:
            return web.json_response({"error": "no session manager"}, status=500)

        new_id = self._new_session_id()
        new_session = self.session_manager.get_or_create(new_id)
        new_session.messages = list(session.messages)
        new_session.metadata = dict(session.metadata)
        new_session.metadata["title"] = f"Fork of {session.metadata.get('title', session_id)}"
        new_session.metadata.pop("revert_point", None)
        new_session.last_consolidated = session.last_consolidated
        self.session_manager.save(new_session)

        info = self._session_to_info(new_session, new_id)
        await self._broadcast_sse("session.created", {"info": info})
        return web.json_response(info)

    # ------------------------------------------------------------------
    # Message endpoints
    # ------------------------------------------------------------------

    async def _handle_session_messages(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        session, key = self._find_session(session_id)
        if not session:
            return web.json_response([], status=200)

        messages = self._messages_to_opencode(session, session_id)
        return web.json_response(messages)

    async def _handle_session_send(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        session, key = self._find_session(session_id)

        if not session:
            # Auto-create session
            key = session_id
            session = self.session_manager.get_or_create(key)
            self.session_manager.save(session)

        if not self.agent_loop:
            return web.json_response({"error": "no agent"}, status=500)

        # If a revert_point is active, permanently truncate before continuing
        revert_point = session.metadata.get("revert_point")
        if isinstance(revert_point, int) and 0 <= revert_point < len(session.messages):
            session.messages = session.messages[:revert_point]
            session.metadata.pop("revert_point", None)
            self.session_manager.save(session)

        body = await request.json()
        active_model = self._session_model(session, body)

        # Extract user text from request
        user_text = ""
        if isinstance(body, dict):
            # { parts: [{ type: "text", text: "..." }] } or { content: "..." }
            parts = body.get("parts", [])
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    user_text = part.get("text", "")
                    break
            if not user_text:
                user_text = body.get("content", body.get("text", ""))

        if not user_text:
            return web.json_response({"error": "empty message"}, status=400)

        now_s = time.time()
        now_ms = self._epoch_ms(now_s)
        provider_name, model_id = self._split_model(active_model)

        # Use deterministic IDs aligned with persisted session index mapping.
        # This avoids TUI reordering/duplication when it reconciles live SSE events
        # with /session/{id}/message history.
        base_index = len(session.messages)
        user_msg_id = f"msg_{session_id}_{base_index}"
        user_part_id = f"part_{session_id}_{base_index}"

        user_msg = {
            "id": user_msg_id,
            "sessionID": session_id,
            "role": "user",
            "time": {"created": now_ms},
            "agent": "default",
            "model": {"providerID": provider_name, "modelID": model_id},
        }
        user_part = {
            "id": user_part_id,
            "sessionID": session_id,
            "messageID": user_msg_id,
            "type": "text",
            "text": user_text,
            "time": {"created": now_ms},
        }

        await self._broadcast_sse("message.updated", {"info": user_msg})
        await self._broadcast_sse("message.part.updated", {"part": user_part})

        # Assistant placeholder ID follows the expected next raw message index.
        asst_msg_id = f"msg_{session_id}_{base_index + 1}"
        asst_part_id = f"part_{session_id}_{base_index + 1}"

        asst_msg = {
            "id": asst_msg_id,
            "sessionID": session_id,
            "role": "assistant",
            "time": {"created": now_ms + 1},
            "parentID": user_msg_id,
            "modelID": model_id,
            "providerID": provider_name,
            "mode": "default",
            "agent": "default",
            "path": {"cwd": str(self.agent_loop.workspace), "root": str(self.agent_loop.workspace)},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        }

        await self._broadcast_sse("message.updated", {"info": asst_msg})
        await self._broadcast_sse(
            "session.status",
            {
                "sessionID": session_id,
                "status": {"type": "busy"},
            },
        )

        # Process via agent
        accumulated_text = []
        tool_part_counter = 0  # Track tool part indices for SSE events

        async def on_progress(
            content: str,
            *,
            tool_hint: bool = False,
            tool_event: dict | None = None,
        ) -> None:
            nonlocal tool_part_counter

            # Handle structured tool lifecycle events
            if tool_event:
                evt_type = tool_event.get("type", "")
                call_id = tool_event.get("call_id", "")
                tool_name = tool_event.get("name", "")
                tool_input = tool_event.get("input", {})

                if evt_type == "tool_start":
                    tool_part_counter += 1
                    tool_title = tool_name
                    first_val = next(iter(tool_input.values()), None) if isinstance(tool_input, dict) else None
                    if isinstance(first_val, str):
                        short = first_val[:40] + "…" if len(first_val) > 40 else first_val
                        tool_title = f'{tool_name}("{short}")'
                    await self._broadcast_sse(
                        "message.part.updated",
                        {
                            "part": {
                                "id": f"{asst_part_id}_tool_{tool_part_counter}",
                                "sessionID": session_id,
                                "messageID": asst_msg_id,
                                "type": "tool",
                                "callID": call_id,
                                "tool": tool_name,
                                "state": {
                                    "status": "running",
                                    "input": tool_input,
                                    "output": "",
                                    "title": tool_title,
                                    "metadata": {},
                                    "time": self._epoch_ms(time.time()),
                                },
                                "time": {"start": self._epoch_ms(time.time())},
                            }
                        },
                    )
                elif evt_type == "tool_done":
                    tool_output = tool_event.get("output", "")
                    is_error = isinstance(tool_output, str) and tool_output.startswith("Error")
                    metadata: dict = {}
                    diff = tool_event.get("diff")
                    if isinstance(diff, dict):
                        metadata = {
                            "path": diff.get("path", ""),
                            "before": diff.get("before", ""),
                            "after": diff.get("after", ""),
                        }
                    await self._broadcast_sse(
                        "message.part.updated",
                        {
                            "part": {
                                "id": f"{asst_part_id}_tool_{tool_part_counter}",
                                "sessionID": session_id,
                                "messageID": asst_msg_id,
                                "type": "tool",
                                "callID": call_id,
                                "tool": tool_name,
                                "state": {
                                    "status": "error" if is_error else "completed",
                                    "input": tool_input,
                                    "output": tool_output,
                                    "title": tool_name,
                                    "metadata": metadata,
                                    "time": self._epoch_ms(time.time()),
                                },
                                "time": {
                                    "start": self._epoch_ms(time.time()),
                                    "end": self._epoch_ms(time.time()),
                                },
                            }
                        },
                    )
                return

            if tool_hint:
                return
            accumulated_text.append(content)
            await self._broadcast_sse(
                "message.part.updated",
                {
                    "part": {
                        "id": asst_part_id,
                        "sessionID": session_id,
                        "messageID": asst_msg_id,
                        "type": "text",
                        "text": "\n".join(accumulated_text),
                        "time": {"created": now_ms + 1},
                    },
                    "delta": content,
                },
            )

        try:
            response = await self.agent_loop.process_direct(
                content=user_text,
                session_key=key,
                channel="opencode",
                chat_id=session_id,
                on_progress=on_progress,
                model=active_model,
            )
        except asyncio.CancelledError:
            response = "Task cancelled."
        except Exception as e:
            logger.exception("OpenCode message processing failed")
            response = f"Error: {e}"

        usage = self.agent_loop.get_last_llm_usage(key) if self.agent_loop else None
        if isinstance(usage, dict):
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            reasoning_tokens = 0
            details = usage.get("completion_tokens_details")
            if isinstance(details, dict):
                rt = details.get("reasoning_tokens")
                if isinstance(rt, int):
                    reasoning_tokens = rt

            if isinstance(prompt_tokens, int):
                asst_msg["tokens"]["input"] = prompt_tokens
            if isinstance(completion_tokens, int):
                asst_msg["tokens"]["output"] = completion_tokens
            asst_msg["tokens"]["reasoning"] = reasoning_tokens

            if isinstance(usage.get("cost"), (int, float)):
                asst_msg["cost"] = float(usage["cost"])

        # Final assistant message
        final_text = response or "\n".join(accumulated_text) or ""
        asst_part_final = {
            "id": asst_part_id,
            "sessionID": session_id,
            "messageID": asst_msg_id,
            "type": "text",
            "text": final_text,
            "time": {"created": now_ms + 1},
        }
        await self._broadcast_sse("message.part.updated", {"part": asst_part_final})

        asst_msg["time"]["completed"] = self._epoch_ms(time.time())
        await self._broadcast_sse("message.updated", {"info": asst_msg})

        # Session idle
        context_stats = self.agent_loop.get_last_context_stats(key) if self.agent_loop else None
        if isinstance(context_stats, dict):
            self._last_context_by_session[session_id] = context_stats

        await self._broadcast_sse(
            "session.status",
            {
                "sessionID": session_id,
                "status": {
                    "type": "idle",
                    "context": self._last_context_by_session.get(session_id, {}),
                },
            },
        )

        # Update session info
        session_info = self._session_to_info(session, session_id)
        await self._broadcast_sse("session.updated", {"info": session_info})

        # Re-emit user turn once at completion to reduce first-turn race misses
        # when a brand-new session is created and prompted immediately.
        await self._broadcast_sse("message.updated", {"info": user_msg})
        await self._broadcast_sse("message.part.updated", {"part": user_part})

        return web.json_response(
            {
                "info": asst_msg,
                "parts": [asst_part_final],
                "context": self._last_context_by_session.get(session_id, {}),
            }
        )

    async def _handle_prompt_async(self, request: web.Request) -> web.Response:
        # Fire-and-forget version of message send
        session_id = request.match_info["id"]
        body = await request.json()

        async def _process():
            # Reuse the send logic
            fake_request = request
            try:
                await self._handle_session_send(fake_request)
            except Exception:
                logger.exception("prompt_async failed")

        task = asyncio.create_task(_process())
        self._active_tasks[session_id] = task
        task.add_done_callback(lambda _: self._active_tasks.pop(session_id, None))
        return web.Response(status=204)

    async def _handle_session_abort(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        task = self._active_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------
    # Revert / Unrevert (undo/redo)
    # ------------------------------------------------------------------

    async def _handle_message_revert(self, request: web.Request) -> web.Response:
        """Revert (undo) — hide messages from the given message onward."""
        session_id = request.match_info["id"]
        message_id = request.match_info["messageId"]
        session, key = self._find_session(session_id)
        if not session:
            return web.json_response({"error": "session not found"}, status=404)

        # Parse index from message ID: msg_{session_id}_{index}
        try:
            index = int(message_id.rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            return web.json_response({"error": "invalid message ID"}, status=400)

        if index < 0 or index >= len(session.messages):
            return web.json_response({"error": "message index out of range"}, status=400)

        session.metadata["revert_point"] = index
        self.session_manager.save(session)

        info = self._session_to_info(session, session_id)
        await self._broadcast_sse("session.updated", {"info": info})
        return web.json_response({"ok": True, "revertPoint": index})

    async def _handle_message_unrevert(self, request: web.Request) -> web.Response:
        """Unrevert (redo) — restore visibility of all messages."""
        session_id = request.match_info["id"]
        session, key = self._find_session(session_id)
        if not session:
            return web.json_response({"error": "session not found"}, status=404)

        session.metadata.pop("revert_point", None)
        self.session_manager.save(session)

        info = self._session_to_info(session, session_id)
        await self._broadcast_sse("session.updated", {"info": info})
        return web.json_response({"ok": True})

    # ------------------------------------------------------------------
    # Permissions
    # ------------------------------------------------------------------

    async def _handle_permission_reply(self, request: web.Request) -> web.Response:
        """Handle user reply to a tool permission request."""
        perm_id = request.match_info["permissionId"]
        future = self._pending_permissions.get(perm_id)
        if not future or future.done():
            return web.json_response({"error": "permission not found or expired"}, status=404)

        try:
            body = await request.json()
        except Exception:
            body = {}

        reply = body.get("reply", "reject") if isinstance(body, dict) else "reject"
        if reply not in ("once", "always", "reject"):
            reply = "reject"

        future.set_result(reply)
        await self._broadcast_sse(
            "permission.replied",
            {"permissionID": perm_id, "reply": reply},
        )
        return web.json_response({"ok": True, "reply": reply})

    async def _permission_callback(
        self, tool_name: str, call_id: str, args: dict
    ) -> str:
        """Async callback invoked by AgentLoop when a tool needs permission.

        Emits a permission.asked SSE event and waits for the user to reply
        via the /permissions/{id} endpoint.
        """
        perm_id = f"perm_{uuid.uuid4().hex[:16]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_permissions[perm_id] = future

        await self._broadcast_sse(
            "permission.asked",
            {
                "permissionID": perm_id,
                "tool": tool_name,
                "callID": call_id,
                "input": args,
            },
        )

        try:
            return await future
        finally:
            self._pending_permissions.pop(perm_id, None)

    async def _handle_session_summarize(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        session, key = self._find_session(session_id)
        if not session:
            return web.json_response({"error": "not found"}, status=404)
        if not self.agent_loop:
            return web.json_response({"error": "no agent"}, status=500)

        try:
            body = await request.json()
        except Exception:
            body = {}
        archive_all = isinstance(body, dict) and bool(body.get("archiveAll", False))

        await self._broadcast_sse(
            "session.status",
            {
                "sessionID": session_id,
                "status": {"type": "busy"},
            },
        )

        result = await self.agent_loop.compact_session(key, archive_all=archive_all)

        before = int(result.get("lastConsolidatedBefore", 0))
        after = int(result.get("lastConsolidatedAfter", 0))
        compacted = max(0, after - before)
        history_entry = (
            result.get("historyEntry") if isinstance(result.get("historyEntry"), str) else ""
        )
        if result.get("ok"):
            if compacted > 0:
                summary_text = f"Context compacted: consolidated {compacted} messages into memory."
                if history_entry:
                    summary_text += f"\n\nSummary:\n{history_entry}"
            else:
                summary_text = (
                    "Context is already compact; no additional messages needed summarization."
                )
        else:
            summary_text = "Context compaction failed."

        now_ms = self._epoch_ms(time.time())
        model_name = self._session_model(session, None)
        provider_name, model_id = self._split_model(model_name)
        note_index = len(session.messages)
        note_msg_id = f"msg_{session_id}_{note_index}"
        note_part_id = f"part_{session_id}_{note_index}"
        note_msg = {
            "id": note_msg_id,
            "sessionID": session_id,
            "role": "assistant",
            "time": {"created": now_ms, "completed": now_ms},
            "modelID": model_id,
            "providerID": provider_name,
            "mode": "default",
            "agent": "default",
            "path": {"cwd": str(self.agent_loop.workspace), "root": str(self.agent_loop.workspace)},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        }
        note_part = {
            "id": note_part_id,
            "sessionID": session_id,
            "messageID": note_msg_id,
            "type": "text",
            "text": summary_text,
            "time": {"created": now_ms},
        }

        session.add_message("assistant", summary_text, compact_event=True)
        self.session_manager.save(session)

        await self._broadcast_sse("message.updated", {"info": note_msg})
        await self._broadcast_sse("message.part.updated", {"part": note_part})

        context_stats = self.agent_loop.get_last_context_stats(key)
        if isinstance(context_stats, dict):
            self._last_context_by_session[session_id] = context_stats

        await self._broadcast_sse(
            "session.status",
            {
                "sessionID": session_id,
                "status": {
                    "type": "idle",
                    "context": self._last_context_by_session.get(session_id, {}),
                },
            },
        )

        session_info = self._session_to_info(session, session_id)
        await self._broadcast_sse("session.updated", {"info": session_info})

        return web.json_response(
            {
                **result,
                "sessionID": session_id,
                "context": self._last_context_by_session.get(session_id, {}),
                "info": note_msg,
                "parts": [note_part],
            }
        )

    # ------------------------------------------------------------------
    # Command endpoints
    # ------------------------------------------------------------------

    _COMMANDS = [
        {
            "name": "clear",
            "description": "Clear session history",
            "source": "command",
            "template": "/clear",
            "hints": [],
        },
        {
            "name": "help",
            "description": "Show available commands",
            "source": "command",
            "template": "/help",
            "hints": [],
        },
    ]

    _COMMANDS_BY_NAME = {c["name"]: c for c in _COMMANDS}

    async def _handle_command_list(self, request: web.Request) -> web.Response:
        return web.json_response(self._COMMANDS)

    async def _handle_session_command(self, request: web.Request) -> web.Response:
        """Execute a slash command by resolving its template and processing as a message."""
        session_id = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        command_name = body.get("command", "") if isinstance(body, dict) else ""
        cmd = self._COMMANDS_BY_NAME.get(command_name)
        if not cmd:
            return web.json_response({"error": f"unknown command: {command_name}"}, status=404)

        # Resolve template — substitute $ARGUMENTS if present
        template = cmd["template"]
        arguments = body.get("arguments", "") if isinstance(body, dict) else ""
        if "$ARGUMENTS" in template:
            template = template.replace("$ARGUMENTS", arguments)

        session, key = self._find_session(session_id)
        if not session:
            key = session_id
            session = self.session_manager.get_or_create(key)
            self.session_manager.save(session)
        if not self.agent_loop:
            return web.json_response({"error": "no agent"}, status=500)

        active_model = self._session_model(session, body)

        try:
            response = await self.agent_loop.process_direct(
                content=template,
                session_key=key,
                channel="opencode",
                chat_id=session_id,
                model=active_model,
            )
        except Exception as e:
            logger.exception("Command execution failed")
            response = f"Error: {e}"

        now_ms = self._epoch_ms(time.time())
        provider_name, model_id = self._split_model(active_model)
        base_index = len(session.messages)
        msg_id = f"msg_{session_id}_{base_index}"
        part_id = f"part_{session_id}_{base_index}"

        asst_msg = {
            "id": msg_id,
            "sessionID": session_id,
            "role": "assistant",
            "time": {"created": now_ms, "completed": now_ms},
            "modelID": model_id,
            "providerID": provider_name,
            "mode": "default",
            "agent": "default",
            "path": {"cwd": str(self.agent_loop.workspace), "root": ""},
            "cost": 0,
            "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        }
        asst_part = {
            "id": part_id,
            "sessionID": session_id,
            "messageID": msg_id,
            "type": "text",
            "text": response or "",
            "time": {"created": now_ms},
        }

        await self._broadcast_sse("message.updated", {"info": asst_msg})
        await self._broadcast_sse("message.part.updated", {"part": asst_part})

        session_info = self._session_to_info(session, session_id)
        await self._broadcast_sse("session.updated", {"info": session_info})
        await self._broadcast_sse(
            "session.status",
            {"sessionID": session_id, "status": {"type": "idle"}},
        )

        return web.json_response({
            "info": asst_msg,
            "parts": [asst_part],
        })

    # ------------------------------------------------------------------
    # Stub endpoints
    # ------------------------------------------------------------------

    async def _handle_stub_list(self, request: web.Request) -> web.Response:
        return web.json_response([])

    async def _handle_stub_dict(self, request: web.Request) -> web.Response:
        return web.json_response({})

    async def _handle_stub_ok(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def _handle_vcs(self, request: web.Request) -> web.Response:
        return web.json_response({"branch": ""})

    async def _handle_path(self, request: web.Request) -> web.Response:
        workspace = str(self.agent_loop.workspace) if self.agent_loop else "~/.nanobot"
        home = str(Path.home())
        return web.json_response(
            {
                "home": home,
                "state": f"{home}/.nanobot",
                "config": f"{home}/.nanobot",
                "worktree": workspace,
                "directory": workspace,
            }
        )

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"healthy": True, "version": "0.1.4"})

    # ------------------------------------------------------------------
    # Data translation
    # ------------------------------------------------------------------

    def _find_session(self, session_id: str) -> tuple[Session | None, str]:
        """Find a session by OpenCode session ID. Returns (session, key)."""
        if not self.session_manager:
            return None, ""

        # Try direct key and prefixed key.
        direct_key = session_id
        prefixed_key = f"opencode:{session_id}"
        sessions = self.session_manager.list_sessions()

        # Check cache first
        session = self.session_manager._cache.get(direct_key)
        if session:
            return session, direct_key
        session = self.session_manager._cache.get(prefixed_key)
        if session:
            return session, prefixed_key

        # Search all sessions for matching ID
        for s in sessions:
            s_key = s.get("key", "")
            if s_key == direct_key:
                return self.session_manager.get_or_create(direct_key), direct_key
            if s_key == prefixed_key:
                return self.session_manager.get_or_create(prefixed_key), prefixed_key
            # Also match by suffix
            if ":" in s_key and s_key.split(":", 1)[1] == session_id:
                return self.session_manager.get_or_create(s_key), s_key

        if session_id == "main":
            return None, "main"
        return None, prefixed_key

    def _session_to_info(self, session: Session, session_id: str) -> dict[str, Any]:
        """Convert a nanobot Session to OpenCode Session.Info format."""
        title = session.metadata.get("title", "")
        if not isinstance(title, str) or not title.strip():
            title = self._default_title_for_session(session)
        created_ts = self._epoch_ms(session.created_at.timestamp())
        updated_ts = self._epoch_ms(session.updated_at.timestamp())

        return {
            "id": session_id,
            "projectID": "nanobot",
            "directory": str(self.agent_loop.workspace) if self.agent_loop else "~/.nanobot",
            "title": title,
            "version": "1",
            "time": {
                "created": created_ts,
                "updated": updated_ts,
            },
        }

    def _messages_to_opencode(self, session: Session, session_id: str) -> list[dict[str, Any]]:
        """Convert nanobot message list to OpenCode MessageV2.WithParts format."""
        result = []
        provider_name, model_id = self._parse_model()
        messages = session.messages

        # Respect revert_point — hide messages at or beyond the cutoff
        revert_point = session.metadata.get("revert_point")
        if isinstance(revert_point, int) and 0 <= revert_point < len(messages):
            messages = messages[:revert_point]

        prev_user_id = None
        prev_user_created_ms: int | None = None

        # Pre-index tool results by tool_call_id for pairing with assistant tool_calls
        tool_results: dict[str, dict[str, Any]] = {}
        for m in messages:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                tool_results[m["tool_call_id"]] = m

        for i, m in enumerate(messages):
            role = m.get("role", "")
            content = m.get("content", "")
            ts = m.get("timestamp", "")
            try:
                created = (
                    self._epoch_ms(datetime.fromisoformat(ts).timestamp())
                    if ts
                    else self._epoch_ms(time.time())
                )
            except (ValueError, TypeError):
                created = self._epoch_ms(time.time())

            msg_id = f"msg_{session_id}_{i}"
            part_id = f"part_{session_id}_{i}"

            if role == "user":
                text = content if isinstance(content, str) else str(content)
                msg = {
                    "info": {
                        "id": msg_id,
                        "sessionID": session_id,
                        "role": "user",
                        "time": {"created": created},
                        "agent": "default",
                        "model": {"providerID": provider_name, "modelID": model_id},
                    },
                    "parts": [
                        {
                            "id": part_id,
                            "sessionID": session_id,
                            "messageID": msg_id,
                            "type": "text",
                            "text": text,
                            "time": {"start": created, "end": created},
                        }
                    ],
                }
                result.append(msg)
                prev_user_id = msg_id
                prev_user_created_ms = created

            elif role == "assistant":
                text = content if isinstance(content, str) else str(content)
                tool_calls = m.get("tool_calls", [])

                # Skip assistant messages with no text and no tool calls
                if not text and not tool_calls:
                    continue

                created_assistant = created
                if prev_user_created_ms is not None and created_assistant <= prev_user_created_ms:
                    created_assistant = prev_user_created_ms + 1

                parts: list[dict[str, Any]] = []
                part_idx = 0

                # Text part (if any)
                if text:
                    parts.append({
                        "id": f"{part_id}_{part_idx}",
                        "sessionID": session_id,
                        "messageID": msg_id,
                        "type": "text",
                        "text": text,
                        "time": {"start": created_assistant, "end": created_assistant},
                    })
                    part_idx += 1

                # Tool parts from tool_calls
                for tc in tool_calls:
                    tc_id = tc.get("id", "")
                    tc_func = tc.get("function", {})
                    tc_name = tc_func.get("name", "unknown")
                    tc_args_raw = tc_func.get("arguments", "{}")
                    try:
                        tc_input = json.loads(tc_args_raw) if isinstance(tc_args_raw, str) else tc_args_raw
                    except (json.JSONDecodeError, TypeError):
                        tc_input = tc_args_raw

                    # Find matching tool result
                    tr = tool_results.get(tc_id)
                    tc_output = ""
                    tc_status = "completed"
                    if tr:
                        tc_output = tr.get("content", "")
                        if isinstance(tc_output, str) and tc_output.startswith("Error"):
                            tc_status = "error"
                    else:
                        tc_status = "pending"

                    tool_title = tc_name
                    first_val = next(iter(tc_input.values()), None) if isinstance(tc_input, dict) else None
                    if isinstance(first_val, str):
                        short = first_val[:40] + "…" if len(first_val) > 40 else first_val
                        tool_title = f'{tc_name}("{short}")'

                    parts.append({
                        "id": f"{part_id}_{part_idx}",
                        "sessionID": session_id,
                        "messageID": msg_id,
                        "type": "tool",
                        "callID": tc_id,
                        "tool": tc_name,
                        "state": {
                            "status": tc_status,
                            "input": tc_input,
                            "output": tc_output,
                            "title": tool_title,
                            "metadata": {},
                            "time": created_assistant,
                        },
                        "time": {"start": created_assistant, "end": created_assistant},
                    })
                    part_idx += 1

                # Fall back to single text part if no parts were generated
                if not parts:
                    parts.append({
                        "id": part_id,
                        "sessionID": session_id,
                        "messageID": msg_id,
                        "type": "text",
                        "text": "",
                        "time": {"start": created_assistant, "end": created_assistant},
                    })

                usage = m.get("usage", {})
                tokens = {
                    "input": usage.get("prompt_tokens", 0),
                    "output": usage.get("completion_tokens", 0),
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                }
                details = usage.get("completion_tokens_details")
                if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
                    tokens["reasoning"] = details["reasoning_tokens"]

                msg = {
                    "info": {
                        "id": msg_id,
                        "sessionID": session_id,
                        "role": "assistant",
                        "time": {"created": created_assistant, "completed": created_assistant},
                        "parentID": prev_user_id or "",
                        "modelID": model_id,
                        "providerID": provider_name,
                        "mode": "default",
                        "agent": "default",
                        "path": {
                            "cwd": str(self.agent_loop.workspace) if self.agent_loop else "",
                            "root": "",
                        },
                        "cost": float(usage.get("cost", 0)) if usage else 0,
                        "tokens": tokens,
                    },
                    "parts": parts,
                }
                result.append(msg)

            # role == "tool" messages are consumed via tool_results dict above

        return result
