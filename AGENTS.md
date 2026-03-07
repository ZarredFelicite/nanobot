# Development Notes

## OpenCode TUI Channel

**Branch**: `feature/memu-qmd-integration`
**Files**: `nanobot/channels/opencode.py`, `nanobot/config/schema.py`, `nanobot/channels/manager.py`, `nanobot/cli/commands.py`

### Overview

Added an HTTP+SSE channel that implements the API surface the [OpenCode TUI](https://github.com/anomalyco/opencode) expects. This lets users run `opencode attach http://127.0.0.1:4096` to use nanobot as the backend for the OpenCode terminal interface.

### Configuration

In `~/.nanobot/config.json` under `channels`:

```json
{
  "channels": {
    "opencode": {
      "enabled": true,
      "port": 4096
    }
  }
}
```

Then run `nanobot gateway -v` and attach with `opencode attach http://127.0.0.1:4096`.

### Architecture

- **`OpenCodeConfig`** (`schema.py`) — Config model with `enabled` (bool) and `port` (int, default 4096).
- **`OpenCodeChannel`** (`channels/opencode.py`) — Extends `BaseChannel`, runs an `aiohttp` web server.
- **`ChannelManager`** (`channels/manager.py`) — Now accepts optional `session_manager` and `agent_loop` params, passed to OpenCodeChannel so it can serve sessions and process messages directly.
- **Gateway command** (`cli/commands.py`) — Passes `session_manager` and `agent` to `ChannelManager`.

### API Endpoints

**Bootstrap (TUI blocks on all 4):**
- `GET /config/providers` — Provider list with models and defaults
- `GET /provider` — Provider array
- `GET /agent` — Agent list (returns single "default" agent)
- `GET /config` — Workspace config (theme, keybinds, etc.)

**Sessions:**
- `POST /session` — Create session
- `GET /session` — List sessions
- `GET /session/{id}` — Get session info
- `PATCH /session/{id}` — Update session (title)
- `DELETE /session/{id}` — Delete session
- `GET /session/{id}/message` — Get message history
- `POST /session/{id}/message` — Send message (triggers agent processing + SSE streaming)
- `POST /session/{id}/abort` — Cancel active processing

**SSE:**
- `GET /event` — Server-Sent Events stream. Emits `server.connected` on connect, heartbeat every 10s, and `message.updated` / `message.part.updated` / `session.status` events during chat.

**Stubs** (return empty responses so TUI doesn't crash):
- `/command`, `/skill`, `/lsp`, `/mcp`, `/formatter`, `/vcs`, `/path`, `/find`, `/file`, `/global/health`, `/permission`, `/question`, etc.

### Model ID Handling

Nanobot stores models as `"provider/model-name"` (e.g. `"anthropic/claude-opus-4-5"`). OpenCode expects the provider ID and model ID to be separate — the models dict is keyed by the short model ID (without provider prefix), and the `default` field uses `"providerID/modelID"` format.

The `_parse_model()` helper splits the full nanobot model string into `(provider_name, short_model_id)` to avoid double-prefixing (which caused `model.split is not a function` errors in the TUI).

### Dependencies

Added `aiohttp>=3.9.0,<4.0.0` to `pyproject.toml`.

### Tests

`tests/test_opencode_api.py` — 19 tests covering bootstrap endpoints, session CRUD, message send, SSE stream, and stub endpoints.
