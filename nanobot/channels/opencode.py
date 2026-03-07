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
    from nanobot.config.schema import AgentDefaults, OpenCodeConfig
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
    ):
        super().__init__(config, bus)
        self.session_manager = session_manager
        self.agent_loop = agent_loop
        self.agent_config = agent_config
        self.port = config.port

        self._sse_clients: list[web.StreamResponse] = []
        self._id_counter = 0
        self._active_tasks: dict[str, asyncio.Task] = {}
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

    def _parse_model(self) -> tuple[str, str]:
        """Return (provider_name, short_model_id) from agent config.

        Nanobot stores models as "provider/model-name" (e.g. "anthropic/claude-opus-4-5").
        OpenCode expects the provider ID and model ID to be separate, with the models
        dict keyed by the short model ID (without provider prefix).
        """
        full_model = self.agent_config.model if self.agent_config else "default"
        provider = self.agent_config.provider if self.agent_config else "nanobot"

        if "/" in full_model:
            prefix, short = full_model.split("/", 1)
            if provider == "auto":
                provider = prefix
            return provider, short

        if provider == "auto":
            provider = "nanobot"
        return provider, full_model

    # ------------------------------------------------------------------
    # BaseChannel interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._app = web.Application()
        self._register_routes(self._app)

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
        app.router.add_post("/session/{id}/prompt_async", self._handle_prompt_async)
        app.router.add_post("/session/{id}/abort", self._handle_session_abort)
        app.router.add_post("/session/{id}/init", self._handle_session_init)
        app.router.add_get("/session/{id}/children", self._handle_stub_list)
        app.router.add_get("/session/{id}/todo", self._handle_stub_list)
        app.router.add_get("/session/{id}/diff", self._handle_stub_list)

        # Stubs
        app.router.add_get("/command", self._handle_stub_list)
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
        provider_name, model_id = self._parse_model()

        provider_data = {
            "id": provider_name,
            "name": provider_name.title(),
            "source": "env",
            "env": [],
            "options": {},
            "models": {
                model_id: {
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
                },
            },
        }

        return web.json_response(
            {
                "providers": [provider_data],
                "default": {"default": f"{provider_name}/{model_id}"},
            }
        )

    async def _handle_provider(self, request: web.Request) -> web.Response:
        provider_name, model_id = self._parse_model()

        return web.json_response(
            [
                {
                    "id": provider_name,
                    "name": provider_name.title(),
                    "source": "env",
                    "env": [],
                    "options": {},
                    "models": {
                        model_id: {
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
                        },
                    },
                }
            ]
        )

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
        provider_name, model_id = self._parse_model()
        return web.json_response(
            {
                "theme": "catppuccin-mocha",
                "keybinds": {},
                "tui": {},
                "model": f"{provider_name}/{model_id}",
                "provider": {
                    provider_name: {
                        "models": {
                            model_id: {},
                        },
                    },
                },
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
            while not resp.task.done():
                await self._sse_write(resp, "server.heartbeat", {})
                await asyncio.sleep(10)
        except asyncio.CancelledError:
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
        return web.json_response({})

    async def _handle_session_init(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

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

        body = await request.json()

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
        provider_name, model_id = self._parse_model()

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

        async def on_progress(content: str, *, tool_hint: bool = False) -> None:
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
            )
        except asyncio.CancelledError:
            response = "Task cancelled."
        except Exception as e:
            logger.exception("OpenCode message processing failed")
            response = f"Error: {e}"

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
        await self._broadcast_sse(
            "session.status",
            {
                "sessionID": session_id,
                "status": {"type": "idle"},
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

        asyncio.create_task(_process())
        return web.Response(status=204)

    async def _handle_session_abort(self, request: web.Request) -> web.Response:
        session_id = request.match_info["id"]
        task = self._active_tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
        return web.json_response({"ok": True})

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

        prev_user_id = None
        prev_user_created_ms: int | None = None

        for i, m in enumerate(session.messages):
            role = m.get("role", "")
            content = m.get("content", "")
            ts = m.get("timestamp", "")
            try:
                from datetime import datetime

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
                if not text:
                    continue
                created_assistant = created
                if prev_user_created_ms is not None and created_assistant <= prev_user_created_ms:
                    created_assistant = prev_user_created_ms + 1
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
                        "cost": 0,
                        "tokens": {
                            "input": 0,
                            "output": 0,
                            "reasoning": 0,
                            "cache": {"read": 0, "write": 0},
                        },
                    },
                    "parts": [
                        {
                            "id": part_id,
                            "sessionID": session_id,
                            "messageID": msg_id,
                            "type": "text",
                            "text": text,
                            "time": {"start": created_assistant, "end": created_assistant},
                        }
                    ],
                }
                result.append(msg)

        return result
