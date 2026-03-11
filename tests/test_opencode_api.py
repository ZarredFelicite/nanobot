"""Tests for OpenCode HTTP+SSE channel."""

import asyncio
import json
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from nanobot.bus.queue import MessageBus
from nanobot.channels.opencode import OpenCodeChannel
from nanobot.config.schema import AgentDefaults, ModelsConfig, OpenCodeConfig
from nanobot.session.manager import Session, SessionManager


@pytest.fixture
def bus():
    return MessageBus()


@pytest.fixture
def session_manager(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sessions").mkdir()
    return SessionManager(workspace)


@pytest.fixture
def agent_config():
    return AgentDefaults(
        model="anthropic/claude-sonnet-4-20250514",
        provider="anthropic",
    )


@pytest.fixture
def mock_agent_loop(tmp_path):
    loop = MagicMock()
    loop.workspace = tmp_path / "workspace"
    loop.process_direct = AsyncMock(return_value="Hello from nanobot!")
    return loop


@pytest.fixture
def channel(bus, session_manager, mock_agent_loop, agent_config):
    config = OpenCodeConfig(enabled=True, port=0)  # port 0 = not used in tests
    return OpenCodeChannel(
        config=config,
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_agent_loop,
        agent_config=agent_config,
    )


@pytest.fixture
def app(channel):
    application = web.Application()
    channel._register_routes(application)
    return application


@pytest.fixture
async def client(app, aiohttp_client):
    return await aiohttp_client(app)


# ------------------------------------------------------------------
# Bootstrap endpoints
# ------------------------------------------------------------------


async def test_config_providers(client):
    resp = await client.get("/config/providers")
    assert resp.status == 200
    data = await resp.json()
    assert "providers" in data
    assert "default" in data
    assert len(data["providers"]) >= 1
    provider = data["providers"][0]
    assert "id" in provider
    assert "models" in provider


async def test_provider(client):
    resp = await client.get("/provider")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    assert "models" in data[0]


async def test_agent(client):
    resp = await client.get("/agent")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)
    assert data[0]["name"] == "default"


async def test_config(client):
    resp = await client.get("/config")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, dict)
    assert isinstance(data["model"], str)
    assert "/" in data["model"]


async def test_config_providers_uses_models_catalog(bus, session_manager, mock_agent_loop):
    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_agent_loop,
        agent_config=AgentDefaults(
            model="anthropic/claude-sonnet-4-20250514", provider="anthropic"
        ),
        models_config=ModelsConfig(
            primary="openrouter/minimax/minimax-m2.5",
            fallbacks=["openrouter/moonshotai/kimi-k2.5", "openrouter/z-ai/glm-5"],
        ),
    )

    app = web.Application()
    channel._register_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/config/providers")
        assert resp.status == 200
        data = await resp.json()
        assert data["default"]["default"] == "openrouter/minimax/minimax-m2.5"
        assert len(data["providers"]) == 1
        models = data["providers"][0]["models"]
        assert "minimax/minimax-m2.5" in models
        assert "moonshotai/kimi-k2.5" in models
        assert "z-ai/glm-5" in models
    finally:
        await client.close()


async def test_config_exposes_all_configured_providers(bus, session_manager, mock_agent_loop):
    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_agent_loop,
        agent_config=AgentDefaults(
            model="anthropic/claude-sonnet-4-20250514", provider="anthropic"
        ),
        models_config=ModelsConfig(
            primary="openrouter/minimax/minimax-m2.5",
            fallbacks=["openai-codex/gpt-5.3-codex", "anthropic/claude-sonnet-4-20250514"],
        ),
    )

    app = web.Application()
    channel._register_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.get("/config")
        assert resp.status == 200
        data = await resp.json()
        assert data["model"] == "openrouter/minimax/minimax-m2.5"
        assert "openrouter" in data["provider"]
        assert "openai-codex" in data["provider"]
        assert "anthropic" in data["provider"]
    finally:
        await client.close()


# ------------------------------------------------------------------
# Session endpoints
# ------------------------------------------------------------------


async def test_create_session(client):
    resp = await client.post("/session")
    assert resp.status == 200
    data = await resp.json()
    assert "id" in data
    assert "title" in data
    assert "time" in data


async def test_list_sessions(client):
    # Create a session first
    await client.post("/session")
    resp = await client.get("/session")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)


async def test_get_session(client):
    create_resp = await client.post("/session")
    session = await create_resp.json()
    sid = session["id"]

    resp = await client.get(f"/session/{sid}")
    assert resp.status == 200
    data = await resp.json()
    assert data["id"] == sid


async def test_delete_session(client):
    create_resp = await client.post("/session")
    session = await create_resp.json()
    sid = session["id"]

    resp = await client.delete(f"/session/{sid}")
    assert resp.status == 200


async def test_session_messages_empty(client):
    create_resp = await client.post("/session")
    session = await create_resp.json()
    sid = session["id"]

    resp = await client.get(f"/session/{sid}/message")
    assert resp.status == 200
    data = await resp.json()
    assert isinstance(data, list)
    assert len(data) == 0


# ------------------------------------------------------------------
# Message send
# ------------------------------------------------------------------


async def test_send_message(client, mock_agent_loop):
    create_resp = await client.post("/session")
    session = await create_resp.json()
    sid = session["id"]

    resp = await client.post(
        f"/session/{sid}/message",
        json={
            "parts": [{"type": "text", "text": "Hello!"}],
        },
    )
    assert resp.status == 200
    mock_agent_loop.process_direct.assert_awaited_once()


async def test_prompt_async_uses_original_request_body(client, mock_agent_loop):
    create_resp = await client.post("/session")
    session = await create_resp.json()
    sid = session["id"]

    resp = await client.post(
        f"/session/{sid}/prompt_async",
        json={"parts": [{"type": "text", "text": "queued hello"}]},
    )
    assert resp.status == 204

    for _ in range(50):
        if mock_agent_loop.process_direct.await_count > 0:
            break
        await asyncio.sleep(0.01)

    assert mock_agent_loop.process_direct.await_count == 1
    kwargs = mock_agent_loop.process_direct.await_args.kwargs
    assert kwargs["content"] == "queued hello"


async def test_send_message_passes_resolved_session_key_to_model_input(
    bus, session_manager, mock_agent_loop
):
    shared_key = "telegram:8281248569"
    session_manager.save(Session(key=shared_key))

    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_agent_loop,
        agent_config=AgentDefaults(
            model="anthropic/claude-sonnet-4-20250514", provider="anthropic"
        ),
    )

    app = web.Application()
    channel._register_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        resp = await client.post(
            "/session/8281248569/message",
            json={"parts": [{"type": "text", "text": "remind me via telegram"}]},
        )
        assert resp.status == 200

        kwargs = mock_agent_loop.process_direct.await_args.kwargs
        assert kwargs["content"] == "remind me via telegram"
        assert kwargs["session_key"] == shared_key
        assert kwargs["channel"] == "opencode"
        assert kwargs["chat_id"] == "8281248569"
    finally:
        await client.close()


async def test_send_message_uses_requested_model(bus, session_manager, mock_agent_loop):
    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_agent_loop,
        agent_config=AgentDefaults(
            model="anthropic/claude-sonnet-4-20250514", provider="anthropic"
        ),
    )

    app = web.Application()
    channel._register_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        create_resp = await client.post("/session")
        session = await create_resp.json()
        sid = session["id"]

        resp = await client.post(
            f"/session/{sid}/message",
            json={
                "parts": [{"type": "text", "text": "Hello!"}],
                "model": {"providerID": "openai-codex", "modelID": "gpt-5.3-codex"},
            },
        )
        assert resp.status == 200
        assert (
            mock_agent_loop.process_direct.await_args.kwargs["model"]
            == "openai-codex/gpt-5.3-codex"
        )
    finally:
        await client.close()


async def test_session_status_returns_context_breakdown(bus, session_manager):
    mock_loop = MagicMock()
    mock_loop.workspace = session_manager.workspace
    mock_loop.process_direct = AsyncMock(return_value="ok")
    mock_loop.get_last_context_stats = MagicMock(
        return_value={
            "model": "openrouter/minimax/minimax-m2.5",
            "budget": 171808,
            "contextTokens": 200000,
            "reserveTokensFloor": 20000,
            "maxOutputTokens": 8192,
            "initial": {"system": 3000, "history": 4000, "current": 50, "total": 7050},
            "final": {"system": 3000, "history": 4000, "current": 50, "total": 7050},
            "compactionPasses": 0,
            "trimmedHistoryMessages": 0,
            "withinBudget": True,
            "usagePercent": 4.1,
        }
    )

    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_loop,
        agent_config=AgentDefaults(model="openrouter/minimax/minimax-m2.5", provider="auto"),
    )

    app = web.Application()
    channel._register_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        create_resp = await client.post("/session")
        session = await create_resp.json()
        sid = session["id"]

        send_resp = await client.post(
            f"/session/{sid}/message",
            json={"parts": [{"type": "text", "text": "hello"}]},
        )
        assert send_resp.status == 200
        send_data = await send_resp.json()
        assert "context" in send_data
        assert send_data["context"]["final"]["total"] == 7050

        status_resp = await client.get(f"/session/status?sessionID={sid}")
        assert status_resp.status == 200
        status = await status_resp.json()
        assert status["sessionID"] == sid
        assert status["status"]["context"]["final"]["total"] == 7050
    finally:
        await client.close()


async def test_send_message_includes_usage_tokens(bus, session_manager):
    mock_loop = MagicMock()
    mock_loop.workspace = session_manager.workspace
    mock_loop.process_direct = AsyncMock(return_value="ok")
    mock_loop.get_last_context_stats = MagicMock(return_value={})
    mock_loop.get_last_llm_usage = MagicMock(
        return_value={
            "prompt_tokens": 123,
            "completion_tokens": 45,
            "completion_tokens_details": {"reasoning_tokens": 7},
            "cost": 0.00123,
        }
    )

    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_loop,
        agent_config=AgentDefaults(model="openrouter/minimax/minimax-m2.5", provider="auto"),
    )

    app = web.Application()
    channel._register_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        create_resp = await client.post("/session")
        session = await create_resp.json()
        sid = session["id"]

        send_resp = await client.post(
            f"/session/{sid}/message",
            json={"parts": [{"type": "text", "text": "hello"}]},
        )
        assert send_resp.status == 200
        data = await send_resp.json()
        assert data["info"]["tokens"]["input"] == 123
        assert data["info"]["tokens"]["output"] == 45
        assert data["info"]["tokens"]["reasoning"] == 7
        assert data["info"]["cost"] == 0.00123
    finally:
        await client.close()


async def test_session_summarize_calls_compaction(bus, session_manager):
    mock_loop = MagicMock()
    mock_loop.workspace = session_manager.workspace
    mock_loop.process_direct = AsyncMock(return_value="ok")
    mock_loop.get_last_context_stats = MagicMock(return_value={"final": {"total": 100}})
    mock_loop.compact_session = AsyncMock(
        return_value={
            "ok": True,
            "archiveAll": False,
            "lastConsolidatedBefore": 0,
            "lastConsolidatedAfter": 12,
            "messageCount": 20,
        }
    )

    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_loop,
        agent_config=AgentDefaults(model="openrouter/minimax/minimax-m2.5", provider="auto"),
    )

    app = web.Application()
    channel._register_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        create_resp = await client.post("/session")
        session = await create_resp.json()
        sid = session["id"]

        resp = await client.post(f"/session/{sid}/summarize", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["ok"] is True
        assert data["sessionID"] == sid
        assert data["info"]["role"] == "assistant"
        assert data["parts"][0]["type"] == "text"
        mock_loop.compact_session.assert_awaited_once_with(sid, archive_all=False)
    finally:
        await client.close()


async def test_session_summarize_id_matches_projected_history(bus, session_manager):
    mock_loop = MagicMock()
    mock_loop.workspace = session_manager.workspace
    mock_loop.process_direct = AsyncMock(return_value="ok")
    mock_loop.get_last_context_stats = MagicMock(return_value={})
    mock_loop.compact_session = AsyncMock(
        return_value={
            "ok": True,
            "archiveAll": False,
            "lastConsolidatedBefore": 0,
            "lastConsolidatedAfter": 3,
            "messageCount": 4,
        }
    )

    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_loop,
        agent_config=AgentDefaults(model="openrouter/minimax/minimax-m2.5", provider="auto"),
    )

    app = web.Application()
    channel._register_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        create_resp = await client.post("/session")
        session = await create_resp.json()
        sid = session["id"]

        stored = session_manager.get_or_create(sid)
        stored.messages = [
            {"role": "user", "content": "check", "timestamp": "2026-03-10T23:58:10"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "exec",
                            "arguments": '{"command": "himalaya envelope list --output json"}',
                        },
                    }
                ],
                "timestamp": "2026-03-10T23:58:11",
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "exec",
                "content": "[]",
                "timestamp": "2026-03-10T23:58:12",
            },
            {
                "role": "assistant",
                "content": "Nothing new",
                "timestamp": "2026-03-10T23:58:13",
            },
        ]
        session_manager.save(stored)

        resp = await client.post(f"/session/{sid}/summarize", json={})
        assert resp.status == 200
        summary = await resp.json()

        history_resp = await client.get(f"/session/{sid}/message")
        history = await history_resp.json()

        assert summary["info"]["id"] == history[-1]["info"]["id"]
    finally:
        await client.close()


async def test_session_command_id_matches_projected_history(bus, session_manager):
    async def fake_process_direct(*, content, session_key, **kwargs):
        session = session_manager.get_or_create(session_key)
        session.add_message("user", content)
        session.messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "exec", "arguments": '{"command": "pwd"}'},
                    }
                ],
                "timestamp": "2026-03-10T23:58:11",
            }
        )
        session.messages.append(
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "exec",
                "content": "/tmp",
                "timestamp": "2026-03-10T23:58:12",
            }
        )
        session.messages.append(
            {
                "role": "assistant",
                "content": "Done",
                "timestamp": "2026-03-10T23:58:13",
            }
        )
        session_manager.save(session)
        return "Done"

    mock_loop = MagicMock()
    mock_loop.workspace = session_manager.workspace
    mock_loop.process_direct = AsyncMock(side_effect=fake_process_direct)

    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_loop,
        agent_config=AgentDefaults(model="openrouter/minimax/minimax-m2.5", provider="auto"),
    )

    app = web.Application()
    channel._register_routes(app)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    try:
        create_resp = await client.post("/session")
        session = await create_resp.json()
        sid = session["id"]

        cmd_resp = await client.post(f"/session/{sid}/command", json={"command": "help"})
        assert cmd_resp.status == 200
        cmd_data = await cmd_resp.json()

        history_resp = await client.get(f"/session/{sid}/message")
        history = await history_resp.json()
        assert cmd_data["info"]["id"] == history[-1]["info"]["id"]
    finally:
        await client.close()


async def test_send_empty_message(client):
    create_resp = await client.post("/session")
    session = await create_resp.json()
    sid = session["id"]

    resp = await client.post(
        f"/session/{sid}/message",
        json={
            "parts": [{"type": "text", "text": ""}],
        },
    )
    assert resp.status == 400


def test_messages_to_opencode_merges_tool_turn(bus, session_manager, mock_agent_loop):
    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_agent_loop,
        agent_config=AgentDefaults(
            model="anthropic/claude-sonnet-4-20250514", provider="anthropic"
        ),
    )

    session = Session(key="session-1")
    session.messages = [
        {
            "role": "user",
            "content": "check inbox",
            "timestamp": "2026-03-10T23:58:10",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "exec",
                        "arguments": '{"command": "himalaya envelope list --output json"}',
                    },
                }
            ],
            "timestamp": "2026-03-10T23:58:11",
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "exec",
            "content": "[]",
            "timestamp": "2026-03-10T23:58:12",
        },
        {
            "role": "assistant",
            "content": "You didn't receive any emails today.",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "timestamp": "2026-03-10T23:58:13",
        },
    ]

    messages = channel._messages_to_opencode(session, "session-1")
    assert len(messages) == 2
    assert messages[0]["info"]["id"] == "msg_session-1_0"
    assert messages[0]["info"]["time"]["completed"] == messages[0]["info"]["time"]["created"]

    assistant = messages[1]
    assert assistant["info"]["id"] == "msg_session-1_1"
    parts = assistant["parts"]
    assert parts[0]["type"] == "tool"
    assert parts[-1]["type"] == "text"
    assert "didn't receive" in parts[-1]["text"]


async def test_sse_write_drops_client_on_closing_transport(
    bus, session_manager, mock_agent_loop, agent_config
):
    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_agent_loop,
        agent_config=agent_config,
    )

    class ClosingStream:
        async def write(self, _: bytes) -> None:
            raise RuntimeError("Cannot write to closing transport")

    client = cast(web.StreamResponse, ClosingStream())
    channel._sse_clients.append(client)

    ok = await channel._sse_write(client, "server.heartbeat", {})
    assert ok is False
    assert client not in channel._sse_clients


async def test_broadcast_sse_timeout_does_not_block_healthy_client(
    bus, session_manager, mock_agent_loop, agent_config
):
    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_agent_loop,
        agent_config=agent_config,
    )
    channel._sse_write_timeout_s = 0.01

    class SlowStream:
        async def write(self, _: bytes) -> None:
            await asyncio.sleep(0.5)

    class FastStream:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        async def write(self, data: bytes) -> None:
            self.writes.append(data)

    slow = cast(web.StreamResponse, SlowStream())
    fast_impl = FastStream()
    fast = cast(web.StreamResponse, fast_impl)
    channel._sse_clients.extend([slow, fast])

    await channel._broadcast_sse("server.heartbeat", {})

    assert fast_impl.writes
    assert slow not in channel._sse_clients


async def test_permission_callback_uses_task_local_session_context(
    bus, session_manager, mock_agent_loop, agent_config
):
    channel = OpenCodeChannel(
        config=OpenCodeConfig(enabled=True, port=0),
        bus=bus,
        session_manager=session_manager,
        agent_loop=mock_agent_loop,
        agent_config=agent_config,
    )

    token1 = channel._permission_session_id.set("session-a")
    task1 = asyncio.create_task(channel._permission_callback("exec", "call-a", {"command": "ls"}))
    channel._permission_session_id.reset(token1)

    token2 = channel._permission_session_id.set("session-b")
    task2 = asyncio.create_task(channel._permission_callback("exec", "call-b", {"command": "pwd"}))
    channel._permission_session_id.reset(token2)

    await asyncio.sleep(0)
    pending_infos = list(channel._pending_permission_info.values())
    session_ids = {info["sessionID"] for info in pending_infos}
    assert session_ids == {"session-a", "session-b"}

    for fut in list(channel._pending_permissions.values()):
        if not fut.done():
            fut.set_result("reject")

    results = await asyncio.gather(task1, task2)
    assert results == ["reject", "reject"]


# ------------------------------------------------------------------
# SSE endpoint
# ------------------------------------------------------------------


async def test_sse_connected(client):
    resp = await client.get("/event")
    assert resp.status == 200
    assert resp.content_type == "text/event-stream"

    # Read the first SSE frame
    line = b""
    async for chunk in resp.content.iter_any():
        line += chunk
        if b"\n\n" in line:
            break

    text = line.decode()
    assert "server.connected" in text


# ------------------------------------------------------------------
# Stub endpoints
# ------------------------------------------------------------------


async def test_stub_command(client):
    resp = await client.get("/command")
    assert resp.status == 200
    assert await resp.json() == []


async def test_stub_lsp(client):
    resp = await client.get("/lsp")
    assert resp.status == 200
    assert await resp.json() == {}


async def test_stub_vcs(client):
    resp = await client.get("/vcs")
    assert resp.status == 200
    data = await resp.json()
    assert "branch" in data


async def test_health(client):
    resp = await client.get("/global/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["healthy"] is True


async def test_path(client):
    resp = await client.get("/path")
    assert resp.status == 200
    data = await resp.json()
    assert "home" in data
    assert "directory" in data


async def test_session_status(client):
    resp = await client.get("/session/status")
    assert resp.status == 200


async def test_abort(client):
    create_resp = await client.post("/session")
    session = await create_resp.json()
    sid = session["id"]

    resp = await client.post(f"/session/{sid}/abort")
    assert resp.status == 200
