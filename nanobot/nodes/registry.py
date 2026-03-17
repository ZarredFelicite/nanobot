"""Node registry: token management, live WS connections, and command dispatch."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web
from loguru import logger


class NodeRegistry:
    """Manages registered nodes, their tokens, and live WebSocket connections."""

    def __init__(self, tokens_path: Path):
        self._path = tokens_path
        self._data: dict[str, Any] = {"nodes": {}}
        self._connections: dict[str, web.WebSocketResponse] = {}
        self._pending_futures: dict[str, asyncio.Future] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to load nodes registry {}: {}", self._path, exc)
                self._data = {"nodes": {}}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._data, indent=2))

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def generate_token(self, node_id: str, name: str = "") -> str:
        """Generate and store a token for *node_id*. Returns the token string."""
        token = f"nb_node_{secrets.token_hex(32)}"
        self._data.setdefault("nodes", {})[node_id] = {
            "name": name or node_id,
            "token": token,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save()
        return token

    def validate_token(self, node_id: str, token: str) -> bool:
        entry = self._data.get("nodes", {}).get(node_id)
        if not entry:
            return False
        return secrets.compare_digest(entry.get("token", ""), token)

    def list_nodes(self) -> list[dict[str, Any]]:
        """Return metadata for all registered nodes (includes online status)."""
        result = []
        for nid, meta in self._data.get("nodes", {}).items():
            result.append(
                {
                    "node_id": nid,
                    "name": meta.get("name", nid),
                    "created_at": meta.get("created_at", ""),
                    "online": nid in self._connections,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def register_connection(self, node_id: str, ws: web.WebSocketResponse) -> None:
        old = self._connections.get(node_id)
        if old is not None and not old.closed:
            logger.warning("Node {} reconnected — closing stale WS", node_id)
            asyncio.ensure_future(old.close())
        self._connections[node_id] = ws
        logger.info("Node {} connected", node_id)

    def unregister_connection(self, node_id: str) -> None:
        self._connections.pop(node_id, None)
        logger.info("Node {} disconnected", node_id)

    @property
    def connected_node_ids(self) -> list[str]:
        return list(self._connections.keys())

    # ------------------------------------------------------------------
    # Command dispatch (gateway → node)
    # ------------------------------------------------------------------

    async def send_command(
        self,
        node_id: str,
        command: str,
        working_dir: str | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:
        """Send an exec command to a node and wait for the result."""
        ws = self._connections.get(node_id)
        if ws is None or ws.closed:
            return {"error": f"Node '{node_id}' is not connected"}

        req_id = uuid.uuid4().hex
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending_futures[req_id] = fut

        payload: dict[str, Any] = {
            "type": "exec",
            "id": req_id,
            "command": command,
            "timeout": timeout,
        }
        if working_dir:
            payload["working_dir"] = working_dir

        try:
            await ws.send_json(payload)
        except Exception as exc:
            self._pending_futures.pop(req_id, None)
            return {"error": f"Failed to send command to node: {exc}"}

        try:
            return await asyncio.wait_for(fut, timeout=timeout + 5)
        except asyncio.TimeoutError:
            self._pending_futures.pop(req_id, None)
            return {"error": f"Command timed out after {timeout}s on node '{node_id}'"}

    def resolve_exec_result(self, req_id: str, result: dict[str, Any]) -> None:
        """Resolve a pending exec future with the node's result."""
        fut = self._pending_futures.pop(req_id, None)
        if fut and not fut.done():
            fut.set_result(result)

    # ------------------------------------------------------------------
    # Chat response dispatch (gateway → node)
    # ------------------------------------------------------------------

    async def send_response(self, node_id: str, req_id: str, content: str) -> None:
        """Send a chat response back to a node."""
        ws = self._connections.get(node_id)
        if ws is None or ws.closed:
            logger.warning("Cannot send response to disconnected node {}", node_id)
            return
        try:
            await ws.send_json({"type": "response", "id": req_id, "content": content})
        except Exception as exc:
            logger.error("Failed to send response to node {}: {}", node_id, exc)
