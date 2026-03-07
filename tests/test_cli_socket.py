"""Tests for CLISocketServer channel."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.cli_socket import CLISocketServer
from nanobot.config.schema import CLISocketConfig


@pytest.fixture
def socket_path(tmp_path):
    return tmp_path / "test_cli.sock"


@pytest.fixture
def bus():
    return MessageBus()


@pytest.fixture
def config(socket_path):
    return CLISocketConfig(socket_path=str(socket_path))


@pytest.fixture
def server(config, bus):
    return CLISocketServer(config, bus, default_session="user:test")


@pytest.fixture
def server_no_default(config, bus):
    return CLISocketServer(config, bus, default_session="")


async def _connect(socket_path):
    """Helper to connect a client and read the welcome message."""
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    welcome_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
    welcome = json.loads(welcome_line.decode().strip())
    return reader, writer, welcome


@pytest.mark.asyncio
async def test_server_lifecycle(server, socket_path):
    """Server start creates socket, stop removes it."""
    await server.start()
    assert socket_path.exists()
    assert server.is_running

    await server.stop()
    assert not socket_path.exists()
    assert not server.is_running


@pytest.mark.asyncio
async def test_client_receives_welcome(server, socket_path):
    """Connected client receives welcome with chatId."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)
        assert welcome["type"] == "welcome"
        assert welcome["chatId"].startswith("cli_")
        assert welcome["defaultSession"] == "user:test"
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_welcome_omits_default_session_when_empty(server_no_default, socket_path, config):
    """Welcome message omits defaultSession when not configured."""
    server_no_default._socket_path = Path(config.socket_path).expanduser()
    await server_no_default.start()
    try:
        reader, writer, welcome = await _connect(socket_path)
        assert "defaultSession" not in welcome
        writer.close()
        await writer.wait_closed()
    finally:
        await server_no_default.stop()


@pytest.mark.asyncio
async def test_message_publishes_to_bus(server, socket_path, bus):
    """Client message is published to the message bus."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)
        chat_id = welcome["chatId"]

        # Send a message
        msg = {"type": "message", "content": "hello world"}
        writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()

        # Consume from bus
        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=2.0)
        assert inbound.channel == "cli"
        assert inbound.chat_id == chat_id
        assert inbound.content == "hello world"
        assert inbound.session_key_override == "user:test"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_session_override(server, socket_path, bus):
    """Client can override session via message field."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)

        msg = {"type": "message", "content": "test", "session": "work:project"}
        writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()

        inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=2.0)
        assert inbound.session_key_override == "work:project"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_multiple_clients_unique_ids(server, socket_path):
    """Multiple clients get unique chat_ids."""
    await server.start()
    try:
        _, w1, welcome1 = await _connect(socket_path)
        _, w2, welcome2 = await _connect(socket_path)

        assert welcome1["chatId"] != welcome2["chatId"]

        w1.close()
        w2.close()
        await w1.wait_closed()
        await w2.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_send_response_to_client(server, socket_path):
    """Server can send response messages to connected client."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)
        chat_id = welcome["chatId"]

        # Send outbound message through the channel
        out_msg = OutboundMessage(channel="cli", chat_id=chat_id, content="Hi there!")
        await server.send(out_msg)

        # Read response
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        data = json.loads(line.decode().strip())
        assert data["type"] == "response"
        assert data["content"] == "Hi there!"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_send_progress_to_client(server, socket_path):
    """Progress messages are sent with type=progress."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)
        chat_id = welcome["chatId"]

        out_msg = OutboundMessage(
            channel="cli", chat_id=chat_id, content="Reading file...",
            metadata={"_progress": True},
        )
        await server.send(out_msg)

        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        data = json.loads(line.decode().strip())
        assert data["type"] == "progress"
        assert data["content"] == "Reading file..."

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_client_disconnect_cleanup(server, socket_path):
    """Disconnected client is removed from _clients."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)
        chat_id = welcome["chatId"]

        # Verify client is tracked
        await asyncio.sleep(0.05)
        assert chat_id in server._clients

        # Disconnect
        writer.close()
        await writer.wait_closed()

        # Give server time to notice disconnect
        await asyncio.sleep(0.1)
        assert chat_id not in server._clients
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_stale_socket_cleanup(server, socket_path):
    """Server removes stale socket file on start."""
    # Create a stale socket file
    socket_path.touch()
    assert socket_path.exists()

    await server.start()
    assert server.is_running

    # Should have replaced the stale file with a real socket
    reader, writer, welcome = await _connect(socket_path)
    assert welcome["type"] == "welcome"

    writer.close()
    await writer.wait_closed()
    await server.stop()


@pytest.mark.asyncio
async def test_invalid_json_handled(server, socket_path):
    """Invalid JSON from client doesn't crash the server."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)

        # Send invalid JSON
        writer.write(b"not json\n")
        await writer.drain()

        # Read error response
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        data = json.loads(line.decode().strip())
        assert data["type"] == "error"

        # Server should still be running
        assert server.is_running

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_empty_content_ignored(server, socket_path, bus):
    """Messages with empty content are not published to bus."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)

        msg = {"type": "message", "content": ""}
        writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()

        # Should time out since nothing was published
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(bus.consume_inbound(), timeout=0.3)

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_mirror_sends_to_matching_session(server, socket_path):
    """Mirror delivers messages from other channels to CLI clients on the same session."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)
        chat_id = welcome["chatId"]

        # Send a message so the client's session is tracked
        msg = {"type": "message", "content": "hi"}
        writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        await asyncio.sleep(0.05)

        # Mirror a Telegram response on the same session
        out_msg = OutboundMessage(
            channel="telegram", chat_id="12345",
            content="Hello from Telegram!",
            session_key="user:test",
        )
        await server.mirror(out_msg)

        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        data = json.loads(line.decode().strip())
        assert data["type"] == "response"
        assert data["content"] == "Hello from Telegram!"
        assert data["from"] == "telegram"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_mirror_skips_different_session(server, socket_path):
    """Mirror does not deliver messages for a different session."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)

        # Client is on default session "user:test"
        # Mirror a message on a different session
        out_msg = OutboundMessage(
            channel="telegram", chat_id="12345",
            content="You should not see this",
            session_key="other:session",
        )
        await server.mirror(out_msg)

        # Should time out — nothing delivered
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readline(), timeout=0.3)

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_mirror_progress_from_other_channel(server, socket_path):
    """Mirrored progress messages include source channel."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)

        out_msg = OutboundMessage(
            channel="telegram", chat_id="12345",
            content="Thinking...",
            metadata={"_progress": True},
            session_key="user:test",
        )
        await server.mirror(out_msg)

        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        data = json.loads(line.decode().strip())
        assert data["type"] == "progress"
        assert data["from"] == "telegram"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_inbound_echo_from_other_channel(server, socket_path, bus):
    """Inbound messages from other channels are echoed to CLI clients on the same session."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)

        # Simulate a Telegram inbound message on the shared session
        telegram_msg = InboundMessage(
            channel="telegram",
            sender_id="12345|zarred",
            chat_id="12345",
            content="how's it going?",
            session_key_override="user:test",
        )
        await bus.publish_inbound(telegram_msg)

        # CLI client should receive the inbound echo
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        data = json.loads(line.decode().strip())
        assert data["type"] == "inbound"
        assert data["content"] == "how's it going?"
        assert data["from"] == "telegram"

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_inbound_echo_skips_cli_messages(server, socket_path, bus):
    """CLI's own inbound messages are not echoed back."""
    await server.start()
    try:
        reader, writer, welcome = await _connect(socket_path)

        cli_msg = InboundMessage(
            channel="cli",
            sender_id="user",
            chat_id=welcome["chatId"],
            content="hello",
            session_key_override="user:test",
        )
        await bus.publish_inbound(cli_msg)

        # Should not receive an echo of our own message
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readline(), timeout=0.3)

        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
