# Development Notes

Branch: `feature/subconscious-memory`

---

## 1. Subconscious Memory System

**Files**: `nanobot/agent/subconscious.py`, `nanobot/agent/qmd.py`, `nanobot/agent/tools/memory_recall.py`, `nanobot/config/schema.py`, `nanobot/agent/context.py`, `nanobot/agent/loop.py`, `nanobot/skills/memory/SKILL.md`

Replaces the old memU integration and MEMORY.md/HISTORY.md system with a hierarchical markdown-based memory that uses qmd for semantic retrieval.

### Architecture
- **SubconsciousConfig**: Pydantic config (extraction model, classifier model, auto-inject budget, batch thresholds, qmd collection name)
- **SubconsciousService**: Async service with buffered extraction, LLM-driven fact extraction into dynamic markdown notes, qmd-backed semantic recall, idle-based conversation summarization, and classifier-gated memory injection
- **QMDClient**: Async subprocess wrapper around the `qmd` CLI — `vsearch()` for fast vector similarity (memory injection), `query()` for full reranking + lexical search (explicit tool use)
- **MemoryRecallTool**: LLM-callable `memory_search` tool delegating to `service.search()` (full reranking)
- **Auto-injection**: Gated by fast LLM classifier (`should_inject()`), memories queried via `qmd vsearch` and appended to user message as tagged text block (preserves prompt caching — system prompt stays stable)
- **ContextBuilder**: Appends memories to user message with `_MEMORY_CONTEXT_TAG`, stripped before saving to session

### Memory Directory Structure
```
~/.nanobot/workspace/memory/
├── entities/          # Dynamic subdirs (people/, machines/, programs/, etc.)
│   ├── people/
│   ├── machines/
│   └── programs/
├── preferences/       # User preferences, workflows
├── decisions/         # Technical decisions with rationale
└── history/           # Daily logs (YYYY-MM-DD.md) + weekly/monthly summaries
```
Structure is fully dynamic — the extraction LLM discovers existing folders via `_list_existing_notes()` and can create/delete files and directories freely. Notes use `[[Name]]` wikilinks for cross-referencing.

### Extraction Flow
1. User/assistant messages are buffered after each turn (skipped for heartbeat sessions)
2. When threshold reached (5 messages or 120s), extraction LLM (gpt-5-mini by default) is called
3. LLM returns structured JSON via tool call: entities (with `path` and `subcategory`), notes (with `path`). Both support `create`/`update`/`delete` actions
4. Markdown files are written/updated/deleted, empty parent dirs cleaned up, qmd reindexes
5. Path traversal protection via `resolve()` check against memory root

### History (Conversation Summarization)
Separated from extraction. Messages are buffered in `_conversation_buffer`. After 30 minutes idle, an LLM summarizes the whole conversation into a daily history file (`history/YYYY-MM-DD.md`) with session ID and timestamp: `[HH:MM] [session_key] summary`.

### Classifier-Gated Injection
Fast LLM call (`classifier_model`, default `gemini-2.0-flash-lite-001`) decides yes/no before running qmd vsearch. Skips injection for greetings, simple questions, math, code syntax, etc. Heartbeat sessions bypass the classifier and injection entirely.

### Contradiction Handling
Extraction LLM sees existing note filenames and is instructed to use action="update" with complete replacement content when facts change. Supports action="delete" to remove stale notes.

Config:
```json
{ "tools": { "subconscious": { "enabled": true, "extractionModel": "openai/gpt-5-mini", "classifierModel": "openrouter/google/gemini-2.0-flash-lite-001" } } }
```

### Removed
- `nanobot/agent/memu_service.py` (MemUBridge)
- `nanobot/agent/tools/memu_retrieve.py` (MemURetrieveTool)
- `tests/test_memu_bridge.py`
- `memu-py` optional dependency
- qmd MCP server config (now invoked directly via CLI)

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

**Sessions:** `POST /session`, `GET /session`, `GET /session/{id}`, `PATCH /session/{id}`, `DELETE /session/{id}`, `GET /session/{id}/message`, `POST /session/{id}/message`, `POST /session/{id}/abort`, `POST /session/{id}/summarize` (context compaction)

**Commands:** `GET /command` — Returns available slash commands (`/clear`, `/help`) in ACP `Command.Info` format for the TUI slash popover

**SSE:** `GET /event` — Emits `server.connected`, heartbeat, `message.updated`, `message.part.updated`, `session.status`, `session.updated`

**Stubs** (empty responses): `/skill`, `/lsp`, `/mcp`, `/formatter`, `/vcs`, `/path`, `/find`, `/file`, `/global/health`, `/permission`, `/question`, etc.

### Slash Commands
- `/clear` — Clears current session history (server-side, exposed via `GET /command`)
- `/compact` — Summarizes and compacts context (TUI built-in → `POST /session/{id}/summarize`)
- `/new` — Creates a new session (TUI built-in, client-side navigation)
- `/help` — Shows available commands (server-side)

### Model ID Handling
Nanobot models are `"provider/model-name"` (e.g. `"anthropic/claude-opus-4-5"`). OpenCode expects separate provider and model IDs. `_parse_model()` splits the full string into `(provider_name, short_model_id)` to avoid double-prefixing that caused `model.split is not a function` in the TUI.

---

## 5. Heartbeat Isolation

**Files**: `nanobot/agent/loop.py`, `nanobot/config/schema.py`, `nanobot/cli/commands.py`

Heartbeat sessions are isolated from the memory system and use a separate model for tool compatibility.

- **No memory recall/write**: Heartbeat sessions (`key == "heartbeat"`) skip the classifier, qmd queries, and subconscious extraction entirely
- **Separate model**: `HeartbeatConfig.model` overrides the default model for heartbeat execution (default model may not support tool use)
- **History window**: Keeps last 5 user/assistant text exchanges for context on recent heartbeats, stripping tool_calls/tool messages that incompatible models reject
- **Session title**: Auto-set to "Heartbeat" for visibility in OpenCode TUI

Config:
```json
{ "gateway": { "heartbeat": { "enabled": true, "intervalS": 1800, "model": "openrouter/minimax/minimax-m2.5" } } }
```

---

## Tests

| File | Coverage |
|------|----------|
| `tests/test_cli_socket.py` | Socket lifecycle, protocol, session routing |
| `tests/test_opencode_api.py` | Bootstrap, session CRUD, message send, SSE, stubs |
| `tests/test_consolidate_offset.py` | Session consolidation, /clear command, cache immutability |
| `tests/test_context_prompt_cache.py` | Memory injection via user message, system prompt stability |
| `tests/test_loop_save_turn.py` | Turn saving, memory tag stripping |
