"""WebSocket handler for the gateway side of node connections."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web
from loguru import logger

if TYPE_CHECKING:
    from nanobot.bus.queue import MessageBus
    from nanobot.nodes.registry import NodeRegistry


class NodeGatewayHandler:
    """Handles inbound WebSocket connections from nanobot nodes.

    Protocol flow:
    1. Node sends ``{"type": "auth", "node_id": "...", "token": "..."}``
    2. Gateway validates and replies ``auth_ok`` or ``auth_fail``
    3. After auth the connection enters a message loop handling:
       - ``exec_result`` — resolves a pending ``send_command`` future
       - ``message`` — publishes an ``InboundMessage`` to the bus
       - ``ping`` / ``pong`` — keepalive
    """

    KEEPALIVE_INTERVAL = 30  # seconds

    def __init__(self, registry: NodeRegistry, bus: MessageBus):
        self.registry = registry
        self.bus = bus

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=self.KEEPALIVE_INTERVAL)
        await ws.prepare(request)

        node_id: str | None = None
        try:
            node_id = await self._authenticate(ws)
            if node_id is None:
                return ws

            self.registry.register_connection(node_id, ws)
            await self._listen(ws, node_id)
        except Exception as exc:
            logger.error("Node WS error (node={}): {}", node_id, exc)
        finally:
            if node_id:
                self.registry.unregister_connection(node_id)

        return ws

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    async def _authenticate(self, ws: web.WebSocketResponse) -> str | None:
        """Wait for the first message which must be an auth payload.

        Returns the node_id on success, ``None`` on failure (WS is closed).
        """
        try:
            raw = await asyncio.wait_for(ws.receive_json(), timeout=10)
        except (asyncio.TimeoutError, Exception):
            await self._send(ws, {"type": "auth_fail", "error": "Auth timeout"})
            await ws.close()
            return None

        if raw.get("type") != "auth":
            await self._send(ws, {"type": "auth_fail", "error": "Expected auth message"})
            await ws.close()
            return None

        node_id = raw.get("node_id", "")
        token = raw.get("token", "")

        if not self.registry.validate_token(node_id, token):
            logger.warning("Auth failed for node_id={}", node_id)
            await self._send(ws, {"type": "auth_fail", "error": "Invalid node_id or token"})
            await ws.close()
            return None

        await self._send(ws, {"type": "auth_ok"})
        return node_id

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    async def _listen(self, ws: web.WebSocketResponse, node_id: str) -> None:
        async for raw_msg in ws:
            if raw_msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                break
            if raw_msg.type == WSMsgType.ERROR:
                logger.error("Node {} WS error: {}", node_id, ws.exception())
                break
            if raw_msg.type != WSMsgType.TEXT:
                continue

            try:
                msg: dict[str, Any] = raw_msg.json()
            except Exception:
                logger.warning("Non-JSON message from node {}", node_id)
                continue

            msg_type = msg.get("type", "")

            if msg_type == "exec_result":
                self.registry.resolve_exec_result(
                    msg.get("id", ""),
                    {
                        "stdout": msg.get("stdout", ""),
                        "stderr": msg.get("stderr", ""),
                        "exit_code": msg.get("exit_code", -1),
                    },
                )

            elif msg_type == "message":
                await self._handle_chat_message(node_id, msg)

            elif msg_type == "ping":
                await self._send(ws, {"type": "pong"})

            elif msg_type == "pong":
                pass  # response to our keepalive

            else:
                logger.debug("Unknown message type '{}' from node {}", msg_type, node_id)

    async def _handle_chat_message(self, node_id: str, msg: dict[str, Any]) -> None:
        """Publish a node user's chat message to the message bus."""
        from nanobot.bus.events import InboundMessage

        content = msg.get("content", "").strip()
        if not content:
            return

        inbound = InboundMessage(
            channel="node",
            sender_id=msg.get("sender", node_id),
            chat_id=node_id,
            content=content,
            session_key_override=f"node:{node_id}",
            metadata={"node_id": node_id, "msg_id": msg.get("id", uuid.uuid4().hex)},
        )
        await self.bus.publish_inbound(inbound)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _send(ws: web.WebSocketResponse, data: dict[str, Any]) -> None:
        if not ws.closed:
            try:
                await ws.send_json(data)
            except Exception:
                pass
