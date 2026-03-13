"""OpenCode TUI HTTP+SSE channel.

Implements the HTTP REST + Server-Sent Events API that the OpenCode TUI
(https://github.com/anomalyco/opencode) expects, allowing it to connect
to nanobot as its backend.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
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

# Map nanobot tool names → OpenCode tool names so the TUI uses the right renderer.
_TOOL_NAME_MAP: dict[str, str] = {
    "edit_file": "edit",
    "write_file": "write",
    "read_file": "read",
    "list_dir": "list",
    "exec": "bash",
    "web_search": "websearch",
    "web_fetch": "webfetch",
}


def _map_tool_input(tool_name: str, raw_input: dict | Any) -> dict:
    """Map nanobot tool arguments to the field names OpenCode renderers expect.

    OpenCode TUI renderers expect specific field names:
    - read:  input.filePath, input.offset, input.limit
    - edit:  input.filePath, input.oldString, input.newString
    - write: input.filePath, input.content
    - bash:  input.command, input.description
    - list:  input.path
    - glob:  input.pattern, input.path
    - grep:  input.pattern, input.path, input.include
    """
    if not isinstance(raw_input, dict):
        return raw_input if isinstance(raw_input, dict) else {}

    if tool_name == "exec":
        return {
            "command": raw_input.get("command", ""),
            "description": raw_input.get("description", ""),
        }

    # read_file, edit_file, write_file: TUI expects "filePath" not "path"
    if tool_name in ("read_file", "edit_file", "write_file"):
        mapped = dict(raw_input)
        if "path" in mapped and "filePath" not in mapped:
            mapped["filePath"] = mapped.pop("path")
        # edit_file: old_text→oldString, new_text→newString
        if tool_name == "edit_file":
            if "old_text" in mapped and "oldString" not in mapped:
                mapped["oldString"] = mapped.pop("old_text")
            if "new_text" in mapped and "newString" not in mapped:
                mapped["newString"] = mapped.pop("new_text")
        # read_file: start_line→offset, end_line→limit (approx mapping)
        if tool_name == "read_file":
            if "start_line" in mapped and "offset" not in mapped:
                mapped["offset"] = mapped.pop("start_line")
            if "end_line" in mapped and "limit" not in mapped:
                mapped["limit"] = mapped.pop("end_line")
        return mapped

    return dict(raw_input)


def _tool_title(oc_name: str, oc_input: dict) -> str:
    """Build a human-readable title for a tool part.

    Mirrors the TUI's getToolInfo subtitle logic — uses the field most
    relevant to the tool type for the display label.
    """
    # Priority keys matching what OpenCode TUI's BasicTool label() function checks
    label_keys = ["description", "query", "url", "filePath", "path", "pattern", "name"]
    for key in label_keys:
        val = oc_input.get(key)
        if isinstance(val, str) and val:
            short = val[:80] + "…" if len(val) > 80 else val
            return f'{oc_name}("{short}")'
    return oc_name


def _build_tool_metadata(tool_name: str, tool_event: dict, tool_output: str = "") -> dict:
    """Build OpenCode-compatible metadata for a tool_done event.

    For bash/exec tools: includes ``output`` so the TUI renders the block view
    with ``$ command`` and output display.
    For edit tools: includes ``diff`` (unified text) and ``filediff``
    (before/after + line counts) so the TUI can render a diff view.
    For write tools: includes ``filepath`` and ``exists``.
    """
    import difflib

    # Bash: metadata.output triggers block rendering in the TUI
    if tool_name == "exec":
        raw_input = tool_event.get("input", {})
        return {
            "output": tool_output,
            "description": raw_input.get("description", "") if isinstance(raw_input, dict) else "",
        }

    diff_data = tool_event.get("diff")
    if not isinstance(diff_data, dict):
        return {}

    file_path = diff_data.get("path", "")
    before = diff_data.get("before", "")
    after = diff_data.get("after", "")

    if tool_name == "edit_file":
        # Compute unified diff text
        before_lines = before.splitlines(keepends=True)
        after_lines = after.splitlines(keepends=True)
        unified = "".join(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=file_path,
                tofile=file_path,
            )
        )
        additions = sum(1 for ln in after_lines if ln not in before_lines)
        deletions = sum(1 for ln in before_lines if ln not in after_lines)
        return {
            "diff": unified,
            "filediff": {
                "file": file_path,
                "before": before,
                "after": after,
                "additions": additions,
                "deletions": deletions,
            },
        }

    if tool_name == "write_file":
        return {
            "filepath": file_path,
            "exists": bool(before),
        }

    return {}


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
        self._id_counter = 0  # Only used by _next_id (kept for potential future use)
        self._active_tasks: dict[str, set[asyncio.Task[Any]]] = {}
        self._pending_permissions: dict[str, asyncio.Future] = {}
        self._pending_permission_info: dict[str, dict[str, Any]] = {}  # perm_id -> metadata
        self._permission_session_id: ContextVar[str] = ContextVar(
            "opencode_permission_session_id", default=""
        )
        self._last_context_by_session: dict[str, dict[str, Any]] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._sse_write_timeout_s = 2.0

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

    def _display_count(self, session: Session, session_id: str) -> int:
        return len(self._messages_to_opencode(session, session_id))

    @staticmethod
    def _ids_for_index(session_id: str, index: int) -> tuple[str, str]:
        return (f"msg_{session_id}_{index}", f"part_{session_id}_{index}")

    def _new_live_ids(self, session_id: str) -> tuple[str, str]:
        return (self._next_id(f"msg_{session_id}"), self._next_id(f"part_{session_id}"))

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
        app.router.add_delete("/session/{id}/message/{messageId}", self._handle_message_revert)
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
        app.router.add_get("/permission", self._handle_permission_list)
        app.router.add_post("/permission/{requestID}/reply", self._handle_permission_reply_v2)
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
        if not await self._sse_write(resp, "server.connected", {}):
            return resp

        try:
            while resp.task is not None and not resp.task.done():
                if not await self._sse_write(resp, "server.heartbeat", {}):
                    break
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

    async def _sse_write(self, resp: web.StreamResponse, event_type: str, properties: dict) -> bool:
        payload = json.dumps({"type": event_type, "properties": properties})
        try:
            await resp.write(f"data: {payload}\n\n".encode())
            return True
        except (ConnectionResetError, ConnectionAbortedError, RuntimeError) as exc:
            logger.debug("OpenCode SSE write failed for {}: {}", event_type, exc)
            if resp in self._sse_clients:
                self._sse_clients.remove(resp)
            return False

    async def _broadcast_sse(self, event_type: str, properties: dict) -> None:
        clients = list(self._sse_clients)
        if not clients:
            return

        writes = [
            asyncio.wait_for(
                self._sse_write(client, event_type, properties),
                timeout=self._sse_write_timeout_s,
            )
            for client in clients
        ]
        results = await asyncio.gather(*writes, return_exceptions=True)
        for client, result in zip(clients, results, strict=False):
            if isinstance(result, Exception):
                logger.debug("OpenCode SSE broadcast failed for {}: {}", event_type, result)
                if client in self._sse_clients:
                    self._sse_clients.remove(client)

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
        try:
            body = await request.json()
        except Exception:
            body = {}
        payload, status = await self._process_session_send(session_id, body)
        return web.json_response(payload, status=status)

    async def _process_session_send(
        self,
        session_id: str,
        body: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], int]:
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_tasks.setdefault(session_id, set()).add(current_task)

        try:
            session, key = self._find_session(session_id)

            if not session:
                if not self.session_manager:
                    return {"error": "no session manager"}, 500
                key = session_id
                session = self.session_manager.get_or_create(key)
                self.session_manager.save(session)

            if not self.agent_loop:
                return {"error": "no agent"}, 500

            if not isinstance(body, dict):
                body = {}

            revert_point = session.metadata.get("revert_point")
            if isinstance(revert_point, int) and 0 <= revert_point < len(session.messages):
                session.messages = session.messages[:revert_point]
                session.metadata.pop("revert_point", None)
                if self.session_manager:
                    self.session_manager.save(session)

            active_model = self._session_model(session, body)

            user_text = ""
            parts = body.get("parts", [])
            for part in parts:
                if isinstance(part, dict) and part.get("type") == "text":
                    user_text = part.get("text", "")
                    break
            if not user_text:
                user_text = body.get("content", body.get("text", ""))

            if not user_text:
                return {"error": "empty message"}, 400

            now_s = time.time()
            now_ms = self._epoch_ms(now_s)
            provider_name, model_id = self._split_model(active_model)
            # Use deterministic index-based IDs so SSE messages match the
            # GET /session/{id}/message response.  The TUI stores messages in
            # a DB keyed by ID and sorted by time_created; mismatched IDs
            # between SSE and GET cause duplicates and ordering glitches.
            display_idx = self._display_count(session, session_id)
            user_msg_id, user_part_id = self._ids_for_index(session_id, display_idx)

            user_msg = {
                "id": user_msg_id,
                "sessionID": session_id,
                "role": "user",
                "time": {"created": now_ms, "completed": now_ms},
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

            asst_msg_id, asst_part_id = self._ids_for_index(session_id, display_idx + 1)
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
                "path": {
                    "cwd": str(self.agent_loop.workspace),
                    "root": str(self.agent_loop.workspace),
                },
                "cost": 0,
                "tokens": {
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                },
            }

            await self._broadcast_sse("message.updated", {"info": asst_msg})
            await self._broadcast_sse(
                "session.status",
                {
                    "sessionID": session_id,
                    "status": {"type": "busy"},
                },
            )

            accumulated_text: list[str] = []
            part_counter = 0
            current_text_part_id = f"{asst_part_id}_p0"
            has_seen_tools = False
            tool_call_part: dict[str, int] = {}

            async def on_progress(
                content: str,
                *,
                tool_hint: bool = False,
                tool_event: dict | None = None,
            ) -> None:
                nonlocal part_counter, current_text_part_id, has_seen_tools

                if tool_event:
                    evt_type = tool_event.get("type", "")
                    call_id = tool_event.get("call_id", "")
                    tool_name = tool_event.get("name", "")
                    tool_input = tool_event.get("input", {})
                    if not isinstance(tool_name, str):
                        tool_name = "unknown"

                    oc_tool_name: str = str(_TOOL_NAME_MAP.get(tool_name) or tool_name)

                    if evt_type == "tool_start":
                        has_seen_tools = True
                        part_counter += 1
                        tool_call_part[call_id] = part_counter
                        oc_input = _map_tool_input(tool_name, tool_input)
                        start_ms = self._epoch_ms(time.time())
                        await self._broadcast_sse(
                            "message.part.updated",
                            {
                                "part": {
                                    "id": f"{asst_part_id}_p{part_counter}",
                                    "sessionID": session_id,
                                    "messageID": asst_msg_id,
                                    "type": "tool",
                                    "callID": call_id,
                                    "tool": oc_tool_name,
                                    "state": {
                                        "status": "running",
                                        "input": oc_input,
                                        "title": _tool_title(oc_tool_name, oc_input),
                                        "metadata": {},
                                        "time": {"start": start_ms},
                                    },
                                }
                            },
                        )
                    elif evt_type == "tool_done":
                        tool_output = tool_event.get("output", "")
                        is_error = isinstance(tool_output, str) and tool_output.startswith("Error")
                        oc_input = _map_tool_input(tool_name, tool_input)
                        metadata = _build_tool_metadata(tool_name, tool_event, tool_output)
                        tc_part_idx = tool_call_part.get(call_id, part_counter)
                        done_ms = self._epoch_ms(time.time())
                        state: dict[str, Any]
                        if is_error:
                            state = {
                                "status": "error",
                                "input": oc_input,
                                "error": tool_output,
                                "metadata": metadata,
                                "time": {"start": done_ms, "end": done_ms},
                            }
                        else:
                            state = {
                                "status": "completed",
                                "input": oc_input,
                                "output": tool_output,
                                "title": _tool_title(oc_tool_name, oc_input),
                                "metadata": metadata,
                                "time": {"start": done_ms, "end": done_ms},
                            }
                        await self._broadcast_sse(
                            "message.part.updated",
                            {
                                "part": {
                                    "id": f"{asst_part_id}_p{tc_part_idx}",
                                    "sessionID": session_id,
                                    "messageID": asst_msg_id,
                                    "type": "tool",
                                    "callID": call_id,
                                    "tool": oc_tool_name,
                                    "state": state,
                                }
                            },
                        )
                    return

                if tool_hint:
                    return

                if has_seen_tools:
                    part_counter += 1
                    current_text_part_id = f"{asst_part_id}_p{part_counter}"
                    accumulated_text.clear()
                    has_seen_tools = False

                accumulated_text.append(content)
                await self._broadcast_sse(
                    "message.part.updated",
                    {
                        "part": {
                            "id": current_text_part_id,
                            "sessionID": session_id,
                            "messageID": asst_msg_id,
                            "type": "text",
                            "text": "\n".join(accumulated_text),
                            "time": {"created": now_ms + 1},
                        },
                        "delta": content,
                    },
                )

            token = self._permission_session_id.set(session_id)
            try:
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
            finally:
                self._permission_session_id.reset(token)

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

            final_text = response or "\n".join(accumulated_text) or ""
            if response and has_seen_tools:
                part_counter += 1
                current_text_part_id = f"{asst_part_id}_p{part_counter}"
            asst_part_final = {
                "id": current_text_part_id,
                "sessionID": session_id,
                "messageID": asst_msg_id,
                "type": "text",
                "text": final_text,
                "time": {"created": now_ms + 1},
            }
            await self._broadcast_sse("message.part.updated", {"part": asst_part_final})

            asst_msg["time"]["completed"] = self._epoch_ms(time.time())
            await self._broadcast_sse("message.updated", {"info": asst_msg})

            # Re-broadcast user message after turn completes to ensure the TUI
            # has it — the initial broadcast may race with optimistic updates.
            await self._broadcast_sse("message.updated", {"info": user_msg})
            await self._broadcast_sse("message.part.updated", {"part": user_part})

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

            session_info = self._session_to_info(session, session_id)
            await self._broadcast_sse("session.updated", {"info": session_info})

            return {
                "info": asst_msg,
                "parts": [asst_part_final],
                "context": self._last_context_by_session.get(session_id, {}),
            }, 200
        finally:
            if current_task is not None:
                session_tasks = self._active_tasks.get(session_id)
                if session_tasks is not None:
                    session_tasks.discard(current_task)
                    if not session_tasks:
                        self._active_tasks.pop(session_id, None)

    async def _handle_prompt_async(self, request: web.Request) -> web.Response:
        # Fire-and-forget version of message send
        session_id = request.match_info["id"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        async def _process():
            try:
                await self._process_session_send(session_id, body)
            except Exception:
                logger.exception("prompt_async failed")

        task = asyncio.create_task(_process())
        self._active_tasks.setdefault(session_id, set()).add(task)

        def _cleanup(done_task: asyncio.Task[Any]) -> None:
            session_tasks = self._active_tasks.get(session_id)
            if session_tasks is not None:
                session_tasks.discard(done_task)
                if not session_tasks:
                    self._active_tasks.pop(session_id, None)

        task.add_done_callback(_cleanup)
        return web.Response(status=204)

    async def _handle_session_abort(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        tasks = self._active_tasks.pop(session_id, set())
        cancelled = 0
        for task in tasks:
            if not task.done() and task.cancel():
                cancelled += 1
        return web.json_response({"ok": True, "cancelled": cancelled})

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
        """Handle user reply to a tool permission request (v1 legacy endpoint)."""
        perm_id = request.match_info["permissionId"]
        return await self._resolve_permission(perm_id, request)

    async def _handle_permission_reply_v2(self, request: web.Request) -> web.Response:
        """Handle user reply to a tool permission request (v2: POST /permission/{requestID}/reply)."""
        perm_id = request.match_info["requestID"]
        return await self._resolve_permission(perm_id, request)

    async def _handle_permission_list(self, request: web.Request) -> web.Response:
        """List pending permission requests."""
        pending = [
            info
            for perm_id, info in self._pending_permission_info.items()
            if perm_id in self._pending_permissions
            and not self._pending_permissions[perm_id].done()
        ]
        return web.json_response(pending)

    async def _resolve_permission(self, perm_id: str, request: web.Request) -> web.Response:
        """Resolve a pending permission future."""
        future = self._pending_permissions.get(perm_id)
        if not future or future.done():
            return web.json_response({"error": "permission not found or expired"}, status=404)

        try:
            body = await request.json()
        except Exception:
            body = {}

        # Support both "reply" (v2) and "response" (v1) field names
        reply = "reject"
        if isinstance(body, dict):
            reply = body.get("reply", body.get("response", "reject"))
        if reply not in ("once", "always", "reject"):
            reply = "reject"

        future.set_result(reply)

        # Get session ID from stored info
        info = self._pending_permission_info.get(perm_id, {})
        session_id = info.get("sessionID", "")

        await self._broadcast_sse(
            "permission.replied",
            {
                "sessionID": session_id,
                "requestID": perm_id,
                "reply": reply,
            },
        )
        return web.json_response(True)

    async def _permission_callback(self, tool_name: str, call_id: str, args: dict) -> str:
        """Async callback invoked by AgentLoop when a tool needs permission.

        Emits a permission.asked SSE event and waits for the user to reply
        via the /permission/{id}/reply endpoint.
        """
        perm_id = f"perm_{uuid.uuid4().hex[:16]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_permissions[perm_id] = future

        # Map tool name to OpenCode permission type
        perm_type_map = {
            "exec": "bash",
            "write_file": "edit",
            "edit_file": "edit",
            "read_file": "read",
            "list_dir": "list",
            "web_search": "websearch",
            "web_fetch": "webfetch",
        }
        permission_type = perm_type_map.get(tool_name, tool_name)

        # Extract patterns (file paths, commands, etc.) from args
        patterns: list[str] = []
        metadata: dict[str, Any] = {}
        if isinstance(args, dict):
            for k in ("path", "filepath", "file"):
                if k in args and isinstance(args[k], str):
                    patterns.append(args[k])
                    metadata["filepath"] = args[k]
                    break
            if "command" in args and isinstance(args["command"], str):
                patterns.append(args["command"])
            if "query" in args and isinstance(args["query"], str):
                patterns.append(args["query"])
            if not patterns:
                # Use first string arg value as pattern
                for v in args.values():
                    if isinstance(v, str):
                        patterns.append(v)
                        break

        session_id = self._permission_session_id.get()

        perm_info = {
            "id": perm_id,
            "sessionID": session_id,
            "permission": permission_type,
            "patterns": patterns,
            "metadata": metadata,
            "always": patterns or ["*"],
            "tool": {
                "callID": call_id,
            },
        }
        self._pending_permission_info[perm_id] = perm_info

        await self._broadcast_sse("permission.asked", perm_info)

        try:
            return await future
        finally:
            self._pending_permissions.pop(perm_id, None)
            self._pending_permission_info.pop(perm_id, None)

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
        note_display_idx = self._display_count(session, session_id)
        note_msg_id, note_part_id = self._ids_for_index(session_id, note_display_idx)
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
        if self.session_manager:
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
            if not self.session_manager:
                return web.json_response({"error": "no session manager"}, status=500)
            key = session_id
            session = self.session_manager.get_or_create(key)
            self.session_manager.save(session)
        if not self.agent_loop:
            return web.json_response({"error": "no agent"}, status=500)

        active_model = self._session_model(session, body)

        token = self._permission_session_id.set(session_id)
        try:
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
        finally:
            self._permission_session_id.reset(token)

        now_ms = self._epoch_ms(time.time())
        provider_name, model_id = self._split_model(active_model)
        # process_direct added messages to session; compute display index
        # for the last assistant message so SSE ID matches GET.
        cmd_display_idx = max(0, self._display_count(session, session_id) - 1)
        msg_id, part_id = self._ids_for_index(session_id, cmd_display_idx)

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

        return web.json_response(
            {
                "info": asst_msg,
                "parts": [asst_part],
            }
        )

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

        def _created_ms(entry: dict[str, Any]) -> int:
            ts = entry.get("timestamp", "")
            try:
                return (
                    self._epoch_ms(datetime.fromisoformat(ts).timestamp())
                    if ts
                    else self._epoch_ms(time.time())
                )
            except (ValueError, TypeError):
                return self._epoch_ms(time.time())

        # Pre-index tool results by tool_call_id for pairing with assistant tool_calls.
        tool_results: dict[str, dict[str, Any]] = {}
        for m in messages:
            if m.get("role") == "tool" and m.get("tool_call_id"):
                tool_results[m["tool_call_id"]] = m

        # Normalize raw history into display messages so tool-call turns map to
        # one assistant message (tools + final text), matching live SSE behavior.
        display_messages: list[dict[str, Any]] = []
        i = 0
        while i < len(messages):
            entry = messages[i]
            role = entry.get("role", "")

            if role == "user":
                display_messages.append(entry)
                i += 1
                continue

            if role != "assistant":
                i += 1
                continue

            tool_calls = entry.get("tool_calls", [])
            content = entry.get("content", "")

            if tool_calls:
                merged = dict(entry)
                text_parts: list[str] = []
                if isinstance(content, str) and content.strip():
                    text_parts.append(content.strip())

                usage_source: dict[str, Any] = entry
                j = i + 1
                while j < len(messages) and messages[j].get("role") != "user":
                    candidate = messages[j]
                    if candidate.get("role") == "assistant" and not candidate.get("tool_calls"):
                        c = candidate.get("content", "")
                        if isinstance(c, str) and c.strip():
                            text_parts.append(c.strip())
                            usage_source = candidate
                        j += 1
                        break
                    j += 1

                merged["content"] = "\n\n".join(text_parts)
                if usage_source is not entry:
                    if usage_source.get("usage") is not None:
                        merged["usage"] = usage_source.get("usage")
                    if usage_source.get("model") is not None:
                        merged["model"] = usage_source.get("model")
                    if usage_source.get("timestamp"):
                        merged["timestamp"] = usage_source.get("timestamp")

                display_messages.append(merged)
                i = j
                continue

            # Plain assistant response.
            if isinstance(content, str) and content.strip():
                display_messages.append(entry)

            i += 1

        for i, m in enumerate(display_messages):
            role = m.get("role", "")
            content = m.get("content", "")
            created = _created_ms(m)

            msg_id = f"msg_{session_id}_{i}"
            part_id = f"part_{session_id}_{i}"

            if role == "user":
                text = content if isinstance(content, str) else str(content)
                msg = {
                    "info": {
                        "id": msg_id,
                        "sessionID": session_id,
                        "role": "user",
                        "time": {"created": created, "completed": created},
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
                continue

            if role != "assistant":
                continue

            text = content if isinstance(content, str) else ""
            tool_calls = m.get("tool_calls", [])
            if not text and not tool_calls:
                continue

            created_assistant = created
            if prev_user_created_ms is not None and created_assistant <= prev_user_created_ms:
                created_assistant = prev_user_created_ms + 1

            parts: list[dict[str, Any]] = []
            part_idx = 0

            for tc in tool_calls:
                tc_id = tc.get("id", "")
                tc_func = tc.get("function", {})
                tc_name = tc_func.get("name", "unknown")
                if not isinstance(tc_name, str):
                    tc_name = "unknown"
                oc_name: str = str(_TOOL_NAME_MAP.get(tc_name) or tc_name)
                tc_args_raw = tc_func.get("arguments", "{}")
                try:
                    tc_input = (
                        json.loads(tc_args_raw) if isinstance(tc_args_raw, str) else tc_args_raw
                    )
                except (json.JSONDecodeError, TypeError):
                    tc_input = tc_args_raw

                tr = tool_results.get(tc_id)
                tc_output = ""
                tc_status = "completed"
                if tr:
                    tc_output = tr.get("content", "")
                    if isinstance(tc_output, str) and tc_output.startswith("Error"):
                        tc_status = "error"
                else:
                    tc_status = "pending"

                oc_input = _map_tool_input(tc_name, tc_input)
                tool_title = _tool_title(oc_name, oc_input)

                tc_metadata: dict[str, Any] = {}
                if tc_name == "exec" and tc_output:
                    tc_metadata = {"output": tc_output}

                if tc_status == "error":
                    tc_state: dict[str, Any] = {
                        "status": "error",
                        "input": oc_input,
                        "error": tc_output,
                        "metadata": tc_metadata,
                        "time": {"start": created_assistant, "end": created_assistant},
                    }
                elif tc_status == "completed":
                    tc_state = {
                        "status": "completed",
                        "input": oc_input,
                        "output": tc_output,
                        "title": tool_title,
                        "metadata": tc_metadata,
                        "time": {"start": created_assistant, "end": created_assistant},
                    }
                else:
                    tc_state = {
                        "status": "pending",
                        "input": oc_input,
                        "raw": "",
                    }

                parts.append(
                    {
                        "id": f"{part_id}_{part_idx}",
                        "sessionID": session_id,
                        "messageID": msg_id,
                        "type": "tool",
                        "callID": tc_id,
                        "tool": oc_name,
                        "state": tc_state,
                    }
                )
                part_idx += 1

            if text:
                parts.append(
                    {
                        "id": f"{part_id}_{part_idx}",
                        "sessionID": session_id,
                        "messageID": msg_id,
                        "type": "text",
                        "text": text,
                        "time": {"start": created_assistant, "end": created_assistant},
                    }
                )
                part_idx += 1

            if not parts:
                parts.append(
                    {
                        "id": part_id,
                        "sessionID": session_id,
                        "messageID": msg_id,
                        "type": "text",
                        "text": "",
                        "time": {"start": created_assistant, "end": created_assistant},
                    }
                )

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

        return result
