"""CLI Unix socket server channel for gateway client mode."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import CLISocketConfig


class CLISocketServer(BaseChannel):
    """Unix domain socket server that lets CLI clients connect to the gateway."""

    name = "cli"

    def __init__(
        self,
        config: CLISocketConfig,
        bus: MessageBus,
        default_session: str = "",
    ):
        super().__init__(config, bus)
        self.config: CLISocketConfig = config
        self.default_session = default_session
        self._socket_path = Path(config.socket_path).expanduser()
        self._server: asyncio.AbstractServer | None = None
        self._clients: dict[str, asyncio.StreamWriter] = {}
        self._client_sessions: dict[str, str] = {}  # chat_id -> session_key
        self._client_counter = 0

    async def start(self) -> None:
        """Start listening on the Unix socket."""
        # Remove stale socket file
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
                logger.debug("Removed stale socket file: {}", self._socket_path)
            except OSError as e:
                logger.error("Cannot remove stale socket {}: {}", self._socket_path, e)
                return

        self._socket_path.parent.mkdir(parents=True, exist_ok=True)

        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        self._running = True
        logger.info("CLI socket listening on {}", self._socket_path)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single CLI client connection."""
        chat_id = f"cli_{self._client_counter}"
        self._client_counter += 1
        self._clients[chat_id] = writer

        # Track session for this client (default until overridden by first message)
        if self.default_session:
            self._client_sessions[chat_id] = self.default_session

        logger.info("CLI client connected: {}", chat_id)

        # Send welcome
        welcome = {"type": "welcome", "chatId": chat_id}
        if self.default_session:
            welcome["defaultSession"] = self.default_session
        self._write_json(writer, welcome)

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break  # Client disconnected

                try:
                    data = json.loads(line.decode().strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._write_json(writer, {"type": "error", "content": "Invalid JSON"})
                    continue

                msg_type = data.get("type", "message")
                if msg_type != "message":
                    continue

                content = data.get("content", "").strip()
                if not content:
                    continue

                # Determine session key
                session_override = data.get("session") or self.default_session or None

                # Track the session this client is using
                if session_override:
                    self._client_sessions[chat_id] = session_override

                msg = InboundMessage(
                    channel="cli",
                    sender_id="user",
                    chat_id=chat_id,
                    content=content,
                    session_key_override=session_override,
                )
                await self.bus.publish_inbound(msg)

        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self._clients.pop(chat_id, None)
            self._client_sessions.pop(chat_id, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("CLI client disconnected: {}", chat_id)

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message to the connected CLI client."""
        writer = self._clients.get(msg.chat_id)
        if not writer:
            logger.debug("CLI client {} not connected, dropping message", msg.chat_id)
            return

        is_progress = msg.metadata.get("_progress", False)
        payload = {
            "type": "progress" if is_progress else "response",
            "content": msg.content or "",
        }
        self._write_json(writer, payload)

    async def mirror(self, msg: OutboundMessage) -> None:
        """Mirror a message from another channel to CLI clients sharing the same session."""
        if not msg.session_key:
            return

        is_progress = msg.metadata.get("_progress", False)
        source = msg.channel

        for chat_id, session in self._client_sessions.items():
            if session != msg.session_key:
                continue
            writer = self._clients.get(chat_id)
            if not writer:
                continue
            payload = {
                "type": "progress" if is_progress else "response",
                "content": msg.content or "",
                "from": source,
            }
            self._write_json(writer, payload)

    async def stop(self) -> None:
        """Stop the server and clean up."""
        self._running = False

        # Close all client connections
        for chat_id, writer in list(self._clients.items()):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._clients.clear()
        self._client_sessions.clear()

        # Close server
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        # Remove socket file
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except OSError:
                pass

        logger.info("CLI socket server stopped")

    @staticmethod
    def _write_json(writer: asyncio.StreamWriter, data: dict) -> None:
        """Write a JSON line to the client."""
        try:
            writer.write(json.dumps(data).encode() + b"\n")
        except Exception:
            pass
