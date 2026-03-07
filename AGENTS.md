# Development Notes

Branch: `feature/memu-qmd-integration`

---

## 1. memU Proactive Memory + qmd MCP Support

**Files**: `nanobot/agent/memu_service.py`, `nanobot/agent/tools/memu_retrieve.py`, `nanobot/config/schema.py`, `nanobot/agent/loop.py`, `shell.nix`

Optional memory backend that extracts structured facts from conversations and enables semantic retrieval via a `memory_search` tool. Keeps existing MEMORY.md system intact.

- **MemUConfig**: Pydantic config (batch thresholds, db path, extraction model)
- **MemUBridge**: Async service with buffered extraction and graceful degradation if `memu-py` not installed
- **MemURetrieveTool**: LLM-callable tool delegating to `bridge.retrieve()`
- **AgentLoop + CLI**: Wire memU lifecycle (init, feed messages, flush, close)
- **shell.nix**: NixOS dev environment with Python 3.13, uv, Node.js, Rust
- **qmd**: Zero-code-change MCP server for hybrid markdown search (docs only)

Config:
```json
{ "tools": { "memu": { "enabled": true, "dbPath": "~/.nanobot/workspace/memory/memu.db" } } }
```

---

## 2. CLI as Gateway Client via Unix Socket

**Files**: `nanobot/channels/cli_socket.py`, `nanobot/config/schema.py`, `nanobot/channels/manager.py`, `nanobot/cli/commands.py`

Unix domain socket channel so `nanobot agent` connects to a running gateway as a thin client, sharing sessions with Telegram and other channels. Falls back to standalone when no gateway is running.

- **CLISocketConfig**: Config with `enabled` and `socket_path` (`~/.nanobot/cli.sock`)
- **CLISocketServer**: Newline-delimited JSON protocol over Unix socket
- **Gateway detection**: CLI auto-detects socket, sends messages, reads responses
- **default_session**: `agents.defaults.session` field for shared session key (e.g. `"user:zarred"`) — moved from `agents.defaultSession` in a refactor commit
- Telegram owner messages route to `default_session` so CLI and Telegram share context

---

## 3. Cross-Channel Mirroring

**Files**: `nanobot/channels/base.py`, `nanobot/channels/cli_socket.py`, `nanobot/channels/telegram.py`, `nanobot/channels/manager.py`, `nanobot/bus/events.py`, `nanobot/agent/loop.py`

Bidirectional mirroring between channels sharing the same session key. When a message arrives on one channel and the agent responds, the response is mirrored to all other channels on the same session.

### Architecture
- **`OutboundMessage.session_key`**: Added to enable cross-channel routing
- **`BaseChannel._session_chat_ids`**: Tracks `session_key → chat_id` from inbound messages
- **`BaseChannel.mirror()`**: Default method that forwards content to matching chat, prefixed with source channel
- **`ChannelManager._dispatch_outbound()`**: After sending to target channel, mirrors to all other channels sharing the same session
- **`MessageBus.add_inbound_listener()`**: Channels can register listeners to echo inbound user messages from other channels

### Telegram-specific
- Overrides `mirror()` to maintain a single editable "CLI Activity" message
- Auto-trims to 30 lines / 4000 chars
- Uses `disable_notification=True` for silent delivery
- Labels mirrored messages as `[cli:user]` or `[cli:agent]`
- Seeds session→chat_id mapping from `allow_from` config at init so mirroring works before first Telegram message

### CLI-specific
- Writes mirrored output directly to tty fd (`os.write`) to bypass prompt_toolkit's stdout proxy
- Shows inbound messages as `[telegram] text` and responses as `← telegram`

### Bug fixes
- Broken pipe from `_test_gateway_connection()` probes handled gracefully in CLISocketServer

---

## 4. OpenCode TUI Channel

**Files**: `nanobot/channels/opencode.py`, `nanobot/config/schema.py`, `nanobot/channels/manager.py`, `nanobot/cli/commands.py`, `pyproject.toml`

HTTP+SSE channel implementing the API surface the [OpenCode TUI](https://github.com/anomalyco/opencode) expects. Allows `opencode attach http://127.0.0.1:4096` to use nanobot as backend.

Config:
```json
{ "channels": { "opencode": { "enabled": true, "port": 4096 } } }
```

### Key Components
- **`OpenCodeConfig`** (`schema.py`) — `enabled` (bool) + `port` (int, default 4096)
- **`OpenCodeChannel`** (`channels/opencode.py`) — Extends `BaseChannel`, runs `aiohttp` web server
- **`ChannelManager`** — Now accepts optional `session_manager` and `agent_loop` params for OpenCodeChannel
- **Dependency**: `aiohttp>=3.9.0,<4.0.0` added to `pyproject.toml`

### API Endpoints

**Bootstrap (TUI blocks on all 4):**
- `GET /config/providers` — Provider list with models and defaults
- `GET /provider` — Provider array
- `GET /agent` — Agent list (single "default" agent)
- `GET /config` — Workspace config

**Sessions:** `POST /session`, `GET /session`, `GET /session/{id}`, `PATCH /session/{id}`, `DELETE /session/{id}`, `GET /session/{id}/message`, `POST /session/{id}/message`, `POST /session/{id}/abort`

**SSE:** `GET /event` — Emits `server.connected`, heartbeat, `message.updated`, `message.part.updated`, `session.status`

**Stubs** (empty responses): `/command`, `/skill`, `/lsp`, `/mcp`, `/formatter`, `/vcs`, `/path`, `/find`, `/file`, `/global/health`, `/permission`, `/question`, etc.

### Model ID Handling
Nanobot models are `"provider/model-name"` (e.g. `"anthropic/claude-opus-4-5"`). OpenCode expects separate provider and model IDs. `_parse_model()` splits the full string into `(provider_name, short_model_id)` to avoid double-prefixing that caused `model.split is not a function` in the TUI.

---

## Tests

| File | Coverage |
|------|----------|
| `tests/test_memu_bridge.py` | memU buffer filtering, no-op, retrieval, tool |
| `tests/test_cli_socket.py` | Socket lifecycle, protocol, session routing |
| `tests/test_opencode_api.py` | Bootstrap, session CRUD, message send, SSE, stubs |
