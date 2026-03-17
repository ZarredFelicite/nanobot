"""Node client: connects to a gateway, handles exec requests, and sends user messages."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import uuid
from pathlib import Path
from typing import Any, Callable

import aiohttp
from loguru import logger


# Default deny patterns (same as ExecTool)
_DEFAULT_DENY_PATTERNS = [
    r"\brm\s+-[rf]{1,2}\b",
    r"\bdel\s+/[fq]\b",
    r"\brmdir\s+/s\b",
    r"(?:^|[;&|]\s*)format\b",
    r"\b(mkfs|diskpart)\b",
    r"\bdd\s+if=",
    r">\s*/dev/sd",
    r"\b(shutdown|reboot|poweroff)\b",
    r":\(\)\s*\{.*\};\s*:",
]


def _guard_command(command: str, deny_patterns: list[str] | None = None) -> str | None:
    """Return an error string if *command* matches a deny pattern, else None."""
    patterns = deny_patterns if deny_patterns is not None else _DEFAULT_DENY_PATTERNS
    lower = command.strip().lower()
    for pattern in patterns:
        if re.search(pattern, lower):
            return "Error: Command blocked by safety guard (dangerous pattern detected)"
    return None


class NodeClient:
    """WebSocket client that connects a node to a nanobot gateway.

    Also runs a local CLI socket server so ``nanobot agent`` on the same
    machine can connect transparently, with messages bridged through the
    gateway WS connection.
    """

    def __init__(
        self,
        gateway_url: str,
        node_id: str,
        token: str,
        deny_patterns: list[str] | None = None,
        on_response: Callable[[str, str], Any] | None = None,
        socket_path: str = "~/.nanobot/cli.sock",
    ):
        self.gateway_url = gateway_url
        self.node_id = node_id
        self.token = token
        self.deny_patterns = deny_patterns if deny_patterns is not None else _DEFAULT_DENY_PATTERNS
        self.on_response = on_response  # callback(msg_id, content)
        self._socket_path = Path(socket_path).expanduser()

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 60.0

        # CLI socket bridge state
        self._cli_server: asyncio.AbstractServer | None = None
        self._cli_clients: dict[str, asyncio.StreamWriter] = {}
        self._cli_counter = 0
        # Track which CLI client sent each message so responses route back
        self._pending_cli: dict[str, str] = {}  # ws msg_id -> cli chat_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, authenticate, and enter the listen loop with auto-reconnect."""
        self._running = True

        # Graceful shutdown on signals
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.ensure_future(self.stop()))
            except NotImplementedError:
                pass

        # Start local CLI socket bridge
        await self._start_cli_socket()

        self._session = aiohttp.ClientSession()
        try:
            while self._running:
                try:
                    await self._connect_and_listen()
                except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                    if not self._running:
                        break
                    logger.warning(
                        "Connection lost ({}), reconnecting in {:.0f}s...",
                        exc,
                        self._reconnect_delay,
                    )
                    await asyncio.sleep(self._reconnect_delay)
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2, self._max_reconnect_delay
                    )
        finally:
            await self._session.close()
            await self._stop_cli_socket()

    async def stop(self) -> None:
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def _connect_and_listen(self) -> None:
        assert self._session is not None
        async with self._session.ws_connect(
            self.gateway_url, heartbeat=30
        ) as ws:
            self._ws = ws

            # Authenticate
            await ws.send_json(
                {"type": "auth", "node_id": self.node_id, "token": self.token}
            )
            auth_resp = await asyncio.wait_for(ws.receive_json(), timeout=10)
            if auth_resp.get("type") != "auth_ok":
                error = auth_resp.get("error", "unknown")
                logger.error("Authentication failed: {}", error)
                raise aiohttp.ClientError(f"Auth failed: {error}")

            logger.info("Connected to gateway as node '{}'", self.node_id)
            self._reconnect_delay = 1.0  # reset backoff on success

            # Listen loop
            async for raw_msg in ws:
                if raw_msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
                if raw_msg.type == aiohttp.WSMsgType.ERROR:
                    break
                if raw_msg.type != aiohttp.WSMsgType.TEXT:
                    continue

                try:
                    msg: dict[str, Any] = raw_msg.json()
                except Exception:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "exec":
                    asyncio.ensure_future(self._handle_exec(ws, msg))
                elif msg_type == "response":
                    self._handle_response(msg)
                elif msg_type == "ping":
                    await ws.send_json({"type": "pong"})

    # ------------------------------------------------------------------
    # Exec handling
    # ------------------------------------------------------------------

    async def _handle_exec(
        self, ws: aiohttp.ClientWebSocketResponse, msg: dict[str, Any]
    ) -> None:
        req_id = msg.get("id", "")
        command = msg.get("command", "")
        working_dir = msg.get("working_dir")
        timeout = msg.get("timeout", 60)

        # Safety guard
        guard_error = _guard_command(command, self.deny_patterns)
        if guard_error:
            await ws.send_json(
                {
                    "type": "exec_result",
                    "id": req_id,
                    "stdout": "",
                    "stderr": guard_error,
                    "exit_code": 1,
                }
            )
            return

        cwd = working_dir or os.getcwd()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                await ws.send_json(
                    {
                        "type": "exec_result",
                        "id": req_id,
                        "stdout": "",
                        "stderr": f"Command timed out after {timeout}s",
                        "exit_code": -1,
                    }
                )
                return

            await ws.send_json(
                {
                    "type": "exec_result",
                    "id": req_id,
                    "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                    "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                    "exit_code": proc.returncode or 0,
                }
            )
        except Exception as exc:
            await ws.send_json(
                {
                    "type": "exec_result",
                    "id": req_id,
                    "stdout": "",
                    "stderr": f"Exec error: {exc}",
                    "exit_code": 1,
                }
            )

    # ------------------------------------------------------------------
    # Chat response handling
    # ------------------------------------------------------------------

    def _handle_response(self, msg: dict[str, Any]) -> None:
        content = msg.get("content", "")
        msg_id = msg.get("id", "")

        # Route to the CLI client that sent this message, if any
        cli_chat_id = self._pending_cli.pop(msg_id, None)
        if cli_chat_id:
            writer = self._cli_clients.get(cli_chat_id)
            if writer and not writer.is_closing():
                self._cli_write_json(writer, {"type": "response", "content": content})
            return

        # Fallback: broadcast to all CLI clients (e.g. unsolicited agent message)
        if self._cli_clients:
            for writer in self._cli_clients.values():
                if not writer.is_closing():
                    self._cli_write_json(writer, {"type": "response", "content": content})
            return

        # No CLI clients — use callback or print
        if self.on_response:
            self.on_response(msg_id, content)
        else:
            print(f"\n[agent] {content}")

    # ------------------------------------------------------------------
    # Send user message to gateway
    # ------------------------------------------------------------------

    async def send_message(self, content: str, sender: str = "user") -> str:
        """Send a chat message to the gateway. Returns the message ID."""
        msg_id = uuid.uuid4().hex
        if self._ws is None or self._ws.closed:
            logger.warning("Not connected to gateway")
            return msg_id
        await self._ws.send_json(
            {
                "type": "message",
                "id": msg_id,
                "content": content,
                "sender": sender,
            }
        )
        return msg_id

    # ------------------------------------------------------------------
    # Local CLI socket bridge
    # ------------------------------------------------------------------

    async def _start_cli_socket(self) -> None:
        """Start a local Unix socket server so `nanobot agent` can connect."""
        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except OSError:
                pass

        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._cli_server = await asyncio.start_unix_server(
            self._handle_cli_client, path=str(self._socket_path)
        )
        logger.info("CLI socket bridge listening on {}", self._socket_path)

    async def _stop_cli_socket(self) -> None:
        """Stop the local CLI socket server."""
        for writer in list(self._cli_clients.values()):
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        self._cli_clients.clear()
        self._pending_cli.clear()

        if self._cli_server:
            self._cli_server.close()
            await self._cli_server.wait_closed()
            self._cli_server = None

        if self._socket_path.exists():
            try:
                self._socket_path.unlink()
            except OSError:
                pass

    async def _handle_cli_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle a single CLI client connection, bridging to the gateway WS."""
        chat_id = f"cli_{self._cli_counter}"
        self._cli_counter += 1
        self._cli_clients[chat_id] = writer

        logger.info("CLI client connected via node bridge: {}", chat_id)

        # Send welcome (same protocol as CLISocketServer)
        self._cli_write_json(writer, {
            "type": "welcome",
            "chatId": chat_id,
            "defaultSession": f"node:{self.node_id}",
        })
        try:
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self._cli_clients.pop(chat_id, None)
            return

        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break

                try:
                    data = json.loads(line.decode().strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._cli_write_json(writer, {"type": "error", "content": "Invalid JSON"})
                    continue

                if data.get("type") != "message":
                    continue

                content = data.get("content", "").strip()
                if not content:
                    continue

                # Forward to gateway over WS
                msg_id = await self.send_message(content)
                self._pending_cli[msg_id] = chat_id

        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._cli_clients.pop(chat_id, None)
            try:
                if not writer.is_closing():
                    writer.close()
                    await writer.wait_closed()
            except Exception:
                pass
            logger.info("CLI client disconnected: {}", chat_id)

    @staticmethod
    def _cli_write_json(writer: asyncio.StreamWriter, data: dict[str, Any]) -> None:
        """Write a JSON line to a CLI client."""
        try:
            writer.write(json.dumps(data).encode() + b"\n")
        except Exception:
            pass
