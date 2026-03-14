# v0.1.4.post4 Manual Port Plan

Goal: manually port the highest-value upstream fixes from `HKUDS/nanobot` release `v0.1.4.post4` into this fork without overwriting the fork's custom architecture (subconscious memory, OpenCode channel, CLI socket gateway, Pi subagent, heartbeat routing).

Working branch: `merge/v0.1.4.post4-fixes`

Notes:
- Do not do a bulk merge from upstream; port each change intentionally.
- Keep fork-specific behavior unless the upstream fix is clearly safer/correcter.
- Preserve unrelated local worktree changes (current repo already has an unrelated deleted `COMMUNICATION.md`).

## Port Order

### 1) Auth allowlist bypass hardening
- Upstream PRs: `#1403`, `#1677`
- Priority: critical
- Why: current `BaseChannel.is_allowed()` still allows `sender_id` token splitting on `|`, which can permit unintended access on non-Telegram channels.
- Local files:
  - `nanobot/channels/base.py`
  - `nanobot/channels/telegram.py`
  - tests to add/update around base-channel and Telegram allowlist behavior
- Plan:
  - remove `|` token splitting from `BaseChannel.is_allowed()`
  - keep Telegram backward compatibility by scoping legacy `id|username` matching to Telegram only
  - verify owner/default-session logic still works with the stricter match behavior
- Validation:
  - targeted tests for exact-match allowlists
  - targeted tests for Telegram legacy `id`, `username`, and `id|username`

### 2) MCP transport + cancellation hardening
- Upstream PRs: `#1488`, `#1728`
- Priority: high
- Why: fork already uses MCP heavily; upstream added SSE auto-detection and prevented leaked `CancelledError` crashes.
- Local files:
  - `nanobot/agent/tools/mcp.py`
  - `nanobot/config/schema.py`
- Plan:
  - add MCP transport type support with SSE auto-detection
  - preserve current streamable HTTP behavior
  - keep graceful timeout behavior and add upstream-style `CancelledError` handling
- Validation:
  - focused tests if practical, otherwise local config/schema sanity + compile checks

### 3) Multi-instance config path support in gateway
- Upstream PR: `#1581`
- Priority: high
- Why: fork runs several channel modes and would benefit from per-instance config/data roots.
- Local files:
  - `nanobot/config/loader.py`
  - `nanobot/utils/helpers.py`
  - `nanobot/cli/commands.py`
- Plan:
  - add configurable config path root
  - make derived data dir track the selected config location
  - thread `--config` into gateway startup before config/data-dir consumers initialize
- Validation:
  - targeted tests in `tests/test_commands.py` if coverage exists
  - manual path-resolution sanity checks

### 4) Agent CLI `--config` / `--workspace`
- Upstream PR: `#1635`
- Priority: high
- Why: complements multi-instance gateway support and is useful for local debugging of this fork.
- Local files:
  - `nanobot/cli/commands.py`
  - `tests/test_commands.py`
- Plan:
  - add `--config/-c` and `--workspace/-w` to `nanobot agent`
  - preserve fork-specific gateway attach behavior
  - match gateway/config precedence as closely as practical
- Validation:
  - CLI-focused tests and help output assertions

### 5) Consecutive user message merge for strict providers
- Upstream PR: `#1456`
- Priority: medium-high
- Why: fork already supports stricter providers; this can reduce provider-side message format failures.
- Local files:
  - `nanobot/agent/context.py`
  - `nanobot/agent/loop.py`
  - existing context/save-turn tests
- Plan:
  - adapt the upstream fix onto the fork's memory-injection approach
  - ensure saved history strips injected runtime/memory context cleanly
  - verify multimodal content still works
- Validation:
  - `tests/test_context_prompt_cache.py`
  - `tests/test_loop_save_turn.py`

### 6) Provider tool-call compatibility review
- Upstream PRs: `#1525`, `#1555`, `#1637`
- Priority: medium
- Why: fork already changed provider/tool-call handling; upstream has useful compatibility fixes for Codex/Copilot ecosystems.
- Local files:
  - `nanobot/providers/litellm_provider.py`
  - any provider-specific files affected by current fork behavior
- Plan:
  - review upstream diffs one by one instead of blind cherry-pick
  - port only compatibility fixes that still apply on top of local provider changes
- Validation:
  - targeted tests if present
  - syntax/compile sanity checks

### 7) Telegram quality-of-life fixes (selective)
- Upstream PRs: `#1476`, `#1482`, `#1522`, `#1535`, `#1660`, `#436`
- Priority: medium
- Why: fork has custom Telegram behavior (mirroring, HTML conversion, routing), so only selective porting is safe.
- Local files:
  - `nanobot/channels/telegram.py`
  - Telegram-related tests
- Plan:
  - review each fix independently
  - port only fixes that do not fight the fork's custom mirror/session behavior
- Validation:
  - targeted Telegram tests where available

## Execution Log

- [x] Step 1 complete: auth allowlist bypass hardening
- [x] Step 2 complete: MCP transport + cancellation hardening
- [x] Step 3 complete: gateway multi-instance config support
- [x] Step 4 complete: agent CLI config/workspace flags
- [x] Step 5 complete: consecutive user message merge
- [x] Step 6 complete: provider tool-call compatibility review
- [x] Step 7 complete: selective Telegram fixes
