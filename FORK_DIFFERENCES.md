# Fork vs Upstream Nanobot

This document describes the major feature and behavior differences between this fork and upstream `HKUDS/nanobot`.

Scope notes:

- This is a code-level comparison of this fork's `main` branch against `origin/main` as checked in this repo.
- It focuses on product behavior, architecture, and developer-facing capabilities, not every small refactor.
- Some changes in this fork are original features; others are selective backports or hardening patches from newer upstream releases.

## Executive Summary

Compared with upstream nanobot, this fork is much more opinionated around a single-user, hackable, coding-assistant workflow.

The biggest differences are:

1. A new hierarchical "subconscious" memory system backed by local markdown notes and `qmd` semantic search, with periodic background nudge reviews for cross-turn memory extraction.
2. A pi-tui terminal UI that `nanobot agent` always launches, connecting to the local gateway or a remote gateway on node machines.
3. Cross-channel session mirroring so CLI, Telegram, and other channels can share one live conversation.
4. A full OpenCode TUI HTTP+SSE backend, including session management, streaming, permissions, revert/unrevert, fork, and context reporting.
5. Stronger heartbeat isolation and delivery rules.
6. Pi subagent integration for delegating larger coding/research tasks to an external coding agent process.
7. OWASP-style prompt-injection hardening for user input, remote content, memory recall, and final output, plus a regex-based secret redaction pipeline applied at tool output and context builder levels.
8. Distributed node mode where remote machines connect to the gateway over WebSocket for remote shell execution and chat routing.
9. Parallel tool execution — read-only and network tools run concurrently via `asyncio.gather` with configurable semaphore limits.
10. Context references — `@file:`, `@folder:`, `@url:`, `@diff`, `@staged`, `@git:N` expansion in user messages with security deny-list and token budget enforcement.
11. Iterative context compression — structured summary templates, tool output pruning, and running summary updates on subsequent compactions.
12. Extra implementation work around MCP cancellation, provider quirks, Telegram owner routing, active-config data paths, and token-aware session compaction.

## High-Level Product Positioning

Upstream nanobot is a lightweight general personal assistant with many channels and providers. This fork keeps that base, but shifts the center of gravity toward:

- persistent personal memory,
- coding-agent workflows,
- multi-client shared sessions,
- TUI/API interoperability,
- and a more stateful single-user setup.

In practice, the fork behaves more like a personal OpenClaw/OpenCode-style agent platform layered on top of nanobot's small core.

## 1. Memory System: Fork Adds "Subconscious" on Top of Upstream's Two-Layer Memory

Primary files:

- `nanobot/agent/subconscious.py`
- `nanobot/agent/qmd.py`
- `nanobot/agent/tools/memory_recall.py`
- `nanobot/agent/context.py`
- `nanobot/agent/loop.py`
- `nanobot/config/schema.py`
- `nanobot/skills/memory/SKILL.md`

### What changed

Upstream nanobot already has a built-in memory system centered on `memory/MEMORY.md` and `memory/HISTORY.md`, managed by `MemoryStore`/`MemoryConsolidator`-style logic.

This fork adds a second, more structured markdown-native system called `subconscious`, and uses it as the primary recall path when enabled. Compared with upstream's default memory flow, the fork now:

- extracts durable facts into structured markdown notes,
- stores them in a dynamic hierarchy under the workspace memory directory,
- indexes them with the local `qmd` CLI,
- auto-injects only relevant memories into the current user turn,
- and exposes explicit semantic recall through a `memory_search` tool.

### Upstream baseline

Upstream memory behavior is simpler and file-oriented:

- `MEMORY.md` stores long-term facts.
- `HISTORY.md` stores grep-friendly historical summaries.
- `MemoryStore` consolidates older conversation chunks into those files through an LLM tool call.
- `ContextBuilder` injects long-term memory into the system prompt.

### New architecture in the fork

- `SubconsciousService` buffers conversation turns, decides when to extract facts, writes notes, reindexes `qmd`, and performs retrieval.
- `QMDClient` wraps the `qmd` CLI asynchronously for semantic retrieval over markdown notes.
- `memory_search` is a first-class tool for explicit recall.
- Recalled memory is appended to the user message with a dedicated context tag, then stripped before persistence so the session transcript stays clean.

### Background extraction and memory writing

The fork does not treat memory as something only updated during explicit summarization. Instead, it runs a background write pipeline during normal conversation flow.

At a high level:

- user/assistant turns are buffered after each exchange,
- once a threshold is reached, the fork asks a lightweight extraction model to decide what durable facts should be written,
- the extractor can create, replace, or delete markdown notes,
- and the `qmd` index is refreshed so those notes become searchable for later recall.

This means memory evolves incrementally during the conversation instead of only being derived from coarse compaction of older turns.

### Memory directory structure

Unlike upstream's simpler pair of memory files, the fork stores extracted knowledge in a dynamic note tree under the workspace memory directory.

Typical top-level buckets include:

- `memory/entities/` for people, machines, programs, and other named things,
- `memory/preferences/` for user preferences and workflows,
- `memory/decisions/` for technical decisions and rationale,
- and `memory/history/` for date-based summaries.

The extractor is not locked to a tiny fixed schema. It can create subdirectories and notes as needed, and uses markdown/wiki-link conventions so knowledge can be cross-linked rather than flattened into one file.

### Recall and semantic search behavior

The fork has two recall modes that upstream does not have in this form:

- automatic recall for prompt construction,
- and explicit recall through the `memory_search` tool.

Automatic recall uses `qmd` vector search to fetch semantically similar memories before a model call. Explicit recall uses a fuller search path intended for agent tool use.

This changes the memory model from "inject the long-term memory file" to "retrieve a relevant slice of structured memory for the current turn."

### Classifier-gated memory injection

The fork does not inject memory unconditionally.

Before doing automatic recall, it first asks a fast classifier model whether memory is likely to help with the current user turn. That lets the fork skip recall for low-value cases such as:

- greetings,
- simple factual questions,
- direct syntax questions,
- or turns where retrieved memory is unlikely to improve the answer.

When the classifier says yes, the fork retrieves relevant notes and appends them to the current user message as tagged memory context. Because the memory context is attached to the user turn rather than the system prompt, the system prompt stays more stable for prompt-caching purposes.

### History summarization vs fact extraction

The fork separates two concepts that upstream largely handles through the simpler memory-file path:

- extracting durable facts,
- and summarizing conversation history.

Durable facts are written into structured notes for future retrieval. Separately, after idle periods, the fork can summarize the recent conversation into dated history notes. This keeps "facts about the user/project" distinct from "what happened in this session."

### Contradiction handling and note replacement

The fork's extractor is designed to update memory notes, not just append new text forever.

That means it can:

- replace stale note content when facts change,
- delete notes that are no longer correct,
- and keep memory closer to a maintained knowledge base than a pure append-only log.

### Behavioral differences from upstream

- Memory is hierarchical and note-based, not just the upstream `MEMORY.md` + `HISTORY.md` pair.
- Recall is semantic and search-driven.
- Injection is classifier-gated, so simple greetings or syntax questions usually skip memory recall.
- The system supports create/update/delete note actions, so contradictions can be resolved by replacing stale memory notes.
- Conversation history summarization is separated from fact extraction.
- The fork still keeps the legacy `MemoryStore` codepath as a fallback/compaction mechanism, but it is no longer the only memory model.

### Important comparison note

This fork should not be described as "upstream used memU" based on the current upstream tree. The observable upstream baseline is the built-in two-file `MEMORY.md`/`HISTORY.md` system. The fork's real difference is that it adds and prioritizes `subconscious` + `qmd` while still retaining the older memory files as compatibility/fallback pieces.

## 2. CLI Launches TUI Connected to Local or Remote Gateway

Primary files:

- `nanobot/cli/commands.py`
- `nanobot/channels/opencode.py`
- `nanobot/channels/manager.py`
- `nanobot/config/schema.py`
- `tui/` (pi-tui TypeScript TUI)

### What changed

Upstream CLI usage is primarily a direct local agent invocation. This fork ships a pi-tui-based terminal UI and makes `nanobot agent` always launch it, connecting to the appropriate gateway.

### Fork behavior

- `nanobot agent` (no `-m` flag) launches the pi-tui TUI.
- On a node machine (where `node.enabled` is true and a node bridge socket exists), the TUI connects to the remote gateway's HTTP API, with host/port extracted from `node.gatewayUrl`.
- On a local gateway machine, the TUI connects to `localhost` on the OpenCode channel port.
- With `-m`, the agent runs in standalone mode for single-shot messages.
- There is no separate Python-based interactive REPL — all interactive sessions go through the pi-tui.

### Why this matters

This gives the fork a unified TUI experience across local and remote setups. A user on a remote node gets the same full-featured interface as someone on the gateway machine, with sessions, streaming, permissions, and all OpenCode API features.

### Important comparison note

Upstream already supports `--config` and `--workspace` on the CLI. The fork-specific difference is not the existence of those flags; it is that the fork combines them with:

- a dedicated TUI that connects to the gateway HTTP API,
- node-aware gateway discovery from `node.gatewayUrl`,
- shared default-session behavior through `agents.defaults.session`,
- and active-config-derived data/workspace path behavior.

That combination makes multi-instance and shared-session workflows more cohesive than in upstream.

## 3. Cross-Channel Session Mirroring

Primary files:

- `nanobot/channels/base.py`
- `nanobot/channels/cli_socket.py`
- `nanobot/channels/telegram.py`
- `nanobot/channels/manager.py`
- `nanobot/bus/events.py`

### What changed

This fork adds bidirectional mirroring across channels that share the same session key.

When a user talks to nanobot from one channel and the agent replies, that traffic can be mirrored into other attached channels for the same session.

### Fork-only capabilities

- outbound messages carry `session_key` so routing can follow the conversation identity,
- channels track which chat IDs correspond to which session,
- the channel manager mirrors replies to sibling channels on the same session,
- channels can also receive mirrored inbound user traffic from other channels.

### User-visible result

- CLI can show Telegram activity.
- Telegram can show CLI-originated activity.
- shared-session workflows feel like one conversation instead of isolated channel silos.

### Telegram-specific mirroring behavior

The fork goes further than a generic mirror for Telegram:

- it keeps a single editable "CLI Activity" message,
- trims the mirrored log to stay within message limits,
- uses silent notifications,
- and seeds owner-session routing from allowlist config so mirroring works before a Telegram message is sent.

## 4. OpenCode TUI Backend and ACP-Style API Surface

Primary files:

- `nanobot/channels/opencode.py`
- `nanobot/channels/manager.py`
- `nanobot/config/schema.py`
- `nanobot/cli/commands.py`
- `pyproject.toml`

### What changed

Upstream nanobot does not ship with this OpenCode-focused HTTP+SSE backend. This fork adds a dedicated channel that implements the API surface expected by the OpenCode TUI.

### Core fork additions

- HTTP server channel for OpenCode attachment.
- bootstrap endpoints for providers, agents, and config.
- SSE event stream for session and message updates.
- session CRUD endpoints.
- message send/list endpoints.
- slash-command discovery endpoint.
- many compatibility stubs for APIs the TUI expects.

### More advanced OpenCode-facing behavior in the fork

This is not just a thin transport layer. The fork also adds OpenCode-style interaction features, including:

- session forking,
- revert/unrevert support,
- async prompt handling,
- session summarization/compaction endpoint,
- permission request/reply flow for sensitive tools,
- file diff metadata for edit tools,
- context stats per session,
- token and cost reporting on assistant messages,
- and model/provider parsing that matches OpenCode's expectations.

### Permission model

The fork introduces `tools.permissions` config and a permission callback path between the agent loop and the OpenCode channel.

That enables an approval workflow for tools like:

- `exec`
- `write_file`
- `edit_file`

This is a meaningful behavioral difference from upstream because the fork can require user approval for certain actions through the TUI transport.

### Why this is a big divergence

This turns nanobot into an attachable backend for an external coding TUI, not just a built-in CLI/chatbot.

## 5. Prompt-Injection Hardening Across Prompt Construction, Remote Content, and Output

Primary files:

- `nanobot/security/prompt_injection.py`
- `nanobot/agent/context.py`
- `nanobot/agent/loop.py`
- `nanobot/agent/tools/web.py`
- `nanobot/channels/email.py`
- `tests/test_prompt_injection.py`

### What changed

Upstream nanobot has general input/tool safeguards, but this fork now adds a dedicated prompt-injection hardening layer inspired by OWASP guidance.

The fork now:

- wraps user input as explicitly untrusted data before it is merged into the prompt,
- wraps recalled memory and remote tool output in "treat as data, not instructions" boundaries,
- sanitizes common remote-content injection patterns before they re-enter model context,
- and validates final assistant output for obvious prompt leakage or secret-like content.

### Covered attack families

The hardening layer explicitly targets:

- direct prompt injection,
- remote/indirect prompt injection from fetched content,
- base64 and hex obfuscation,
- typoglycemia variants,
- best-of-N spacing/casing variants,
- HTML/Markdown exfiltration attempts,
- scratchpad or forged tool-output text such as `Thought:` / `Action:`,
- and prompt-extraction phrases like asking for the exact hidden instructions.

### User-visible behavioral differences

- memory recall is still appended to the user turn, but it is now marked as untrusted content instead of raw plain text,
- `web_search` and `web_fetch` output are no longer passed back verbatim into the model loop,
- inbound email text is wrapped/sanitized before becoming conversation content,
- and obviously suspicious final output is replaced with a refusal instead of being shown verbatim.

### Validation

This fork includes OWASP-style regression coverage in `tests/test_prompt_injection.py`, plus end-to-end prompt-construction and email-ingestion checks in `tests/test_context_prompt_cache.py` and `tests/test_email_channel.py`.

## 6. Heartbeat Is More Isolated and Better Integrated with Shared Sessions

Primary files:

- `nanobot/agent/loop.py`
- `nanobot/cli/commands.py`
- `nanobot/heartbeat/service.py`
- `nanobot/config/schema.py`

### What changed

The fork reworks heartbeat behavior so it is safer and more compatible with the rest of the system.

### Fork-specific behavior

- heartbeat can use its own model via `gateway.heartbeat.model`,
- heartbeat sessions are isolated from subconscious memory extraction and recall,
- recent heartbeat history is pruned down to a small text-only window for compatibility,
- heartbeat output can be persisted back into the main user session,
- and heartbeat delivery can target shared-session channels instead of acting like a completely separate conversation.

### Why this matters

In the fork, heartbeat is treated as an operational background agent mode with stricter boundaries, rather than just another turn in the normal memory pipeline.

## 7. Pi Subagent Integration for Large Tasks

Primary files:

- `nanobot/agent/tools/subagent.py`
- `nanobot/agent/loop.py`

Related existing background execution remains in:

- `nanobot/agent/subagent.py`
- `nanobot/agent/tools/spawn.py`

### What changed

This fork adds a new `subagent` tool that delegates work to an external Pi coding agent subprocess over JSONL RPC.

### Fork-only capabilities

- launches `pi --mode rpc --session ...`,
- keeps separate Pi session files,
- injects extra system prompts like `TOOLS.md`, `USER.md`, and `PI-AGENTS.md`,
- allows provider/model passthrough,
- and documents recommended model tiers for different task sizes.

### Why this differs from upstream

Upstream nanobot has subagent/background concepts, but this fork explicitly integrates another coding-agent runtime as a delegated worker. That is a stronger external-agent integration layer than upstream's default behavior.

## 8. Session Handling, Compaction, and Prompt/Context Behavior Differ

Primary files:

- `nanobot/agent/loop.py`
- `nanobot/session/manager.py`
- `nanobot/agent/context.py`
- `tests/test_consolidate_offset.py`
- `tests/test_context_prompt_cache.py`
- `tests/test_loop_save_turn.py`

### Main differences

The fork puts much more emphasis on cache-friendly prompt construction and session compaction mechanics.

### Fork-specific changes

Upstream already has `last_consolidated` and background memory consolidation. The fork extends that baseline with:

- token-budget-aware context trimming before model calls,
- explicit `compact_session()` support exposed to channels like OpenCode,
- memory injection added to the user message instead of the system prompt,
- richer per-session context stats and recent LLM usage snapshots,
- and heartbeat-specific history shaping that strips incompatible tool-call turns.

### Why this matters

The fork is optimized more aggressively for:

- prompt caching stability,
- preserving recent turns while compacting older ones,
- and surfacing context/token state to clients.

That makes it more suitable for long-running coding sessions than the simpler upstream flow.

## 9. MCP Runtime Behavior Differs From Upstream

Primary files:

- `nanobot/agent/tools/mcp.py`
- `nanobot/config/schema.py`

### Fork differences

Upstream already supports typed MCP config and streamable HTTP. The fork extends the runtime behavior around transport handling and failures.

Notable changes include:

- SSE transport support,
- transport auto-detection,
- tool-call cancellation handling,
- and safer exception handling around MCP failures.

### Practical impact

The fork is more tolerant of different real-world MCP server setups, especially remote/SSE-style servers and cancellation-heavy coding workflows.

## 10. Provider Compatibility Tweaks in the Current Diff

Primary file:

- `nanobot/providers/litellm_provider.py`

### Fork differences

This fork includes extra provider compatibility work, but the current upstream base already contains some related hardening. The observable differences here are narrower than a first glance suggests.

Examples visible in the current diff:

- a different `tool_call_id` compatibility strategy that preserves more of long IDs instead of hashing them down to a short fixed token,
- provider-specific normalization for stricter StepFun tool-call parsing,
- and some request sanitization/parameter handling differences around LiteLLM-backed models.

### Why it matters

This fork is more robust when using non-identical OpenAI-style provider implementations, especially coding-oriented providers that do not perfectly match the expected response schema.

## 11. Telegram and Channel Routing Are More Opinionated

Primary files:

- `nanobot/channels/telegram.py`
- `nanobot/channels/base.py`
- `nanobot/agent/tools/message.py`
- `nanobot/agent/tools/cron.py`

### Fork differences

Compared with upstream, this fork has more single-owner and shared-session routing behavior around Telegram.

Upstream already has topic-aware session metadata, `/stop` forwarding, and proxy support. The fork-specific differences include:

- owner/default-session routing,
- Telegram-only legacy `id|username` allowlist matching layered on top of stricter base-channel matching,
- mirrored cross-channel activity via a single editable Telegram log message,
- and owner-targeted routing behavior for tools like `message` and `cron`.

### Behavioral implication

The fork treats Telegram as a first-class personal control surface, not just another generic chat adapter.

## 11. Config and Multi-Instance Support Are Better Developed

Primary files:

- `nanobot/config/loader.py`
- `nanobot/utils/helpers.py`
- `nanobot/cli/commands.py`

### Fork differences

Upstream already tracks a current config path, but this fork goes further by deriving data/workspace roots from the active config location, enabling cleaner multi-instance setups.

Practical improvements include:

- data/workspace roots derived from the active config file,
- gateway/client behavior that follows the selected config instance,
- and more predictable behavior when running multiple nanobot instances side by side.

This is especially useful for personal deployments where separate profiles or environments need isolated state.

## 12. OpenCode/ACP-Oriented Tooling Metadata and Diffs

Primary files:

- `nanobot/agent/tools/filesystem.py`
- `nanobot/channels/opencode.py`

### Fork differences

The fork captures richer edit metadata than upstream for client rendering.

Examples:

- filesystem edit/write tools keep before/after content snapshots,
- unified diffs are computed for edit events,
- and the OpenCode channel exposes those diffs in a format the TUI can render.

That is important for a coding UI, but it is not part of upstream nanobot's default feature set.

## 13. Developer Experience and Local Environment Support

Primary files:

- `shell.nix`
- `uv.lock`
- `pyproject.toml`

### Fork differences

The fork adds extra project-local environment support and dependency pinning that are not part of the upstream baseline in the same way.

Examples:

- a `shell.nix` for Nix development,
- a committed `uv.lock`,
- and additional dependency updates such as `aiohttp` for the OpenCode channel.

This is not a user-facing feature in the same sense as memory or channels, but it is still a meaningful divergence in how the fork is meant to be developed and run.

## 14. Distributed Node Mode for Multi-Machine Operation

Primary files:

- `nanobot/nodes/registry.py`
- `nanobot/nodes/gateway_ws.py`
- `nanobot/nodes/channel.py`
- `nanobot/nodes/client.py`
- `nanobot/agent/tools/remote_exec.py`
- `nanobot/config/schema.py`
- `nanobot/cli/commands.py`

### What changed

This fork adds an OpenClaw-inspired distributed mode where remote nanobot node instances connect to a gateway over WebSocket. This enables two capabilities:

1. **Remote execution**: The gateway agent gets a `remote_exec` tool that dispatches shell commands to connected nodes.
2. **Node as channel**: Users on node machines run `nanobot node` which routes their messages through the gateway agent — the gateway knows which node the message came from.

### Architecture

- Nodes connect outbound to the gateway (no port forwarding needed on the node side).
- The gateway runs a WebSocket endpoint (`/ws/node`) on the OpenCode channel's aiohttp app.
- A `NodeRegistry` manages token-based authentication, live WebSocket connections, and command dispatch via async futures.
- Each connected node is registered as a dynamic channel (`node`) in the message bus.
- The standard message bus flow handles routing: `NodeGatewayHandler` publishes `InboundMessage` to the bus, and `NodeChannel` routes `OutboundMessage` back over the correct node's WebSocket.

### Token-based authentication

Tokens are generated via `nanobot node-token <id>` and stored in `~/.nanobot/nodes.json`. The WebSocket handshake requires a valid `node_id` + `token` pair before any commands or messages are accepted.

### Safety

The `remote_exec` tool applies the same deny-pattern safety guards as the local `exec` tool on both the gateway side and the node client side. Dangerous commands like `rm -rf` are blocked before they ever reach the remote machine.

### CLI commands

Three new CLI commands:

- `nanobot node-token <id>` — generate auth token
- `nanobot nodes` — list registered nodes with online status
- `nanobot node` — start a node client with auto-reconnect (creates a CLI socket bridge so `nanobot agent` can launch the TUI connected to the remote gateway)

### Why this differs from upstream

Upstream nanobot has no distributed execution capability. This fork explicitly targets multi-machine personal setups (Raspberry Pis, servers, dev boxes) managed from a single gateway agent.

## 15. Test Coverage Added for Fork-Specific Behavior

Primary files:

- `tests/test_cli_socket.py`
- `tests/test_opencode_api.py`
- `tests/test_multi_instance_paths.py`
- `tests/test_litellm_provider.py`
- `tests/test_mcp_config.py`
- `tests/test_telegram_channel.py`
- `tests/test_channel_allowlist.py`
- `tests/test_session_manager.py`
- `tests/test_context_prompt_cache.py`
- `tests/test_loop_save_turn.py`

### Fork differences

The fork ships tests specifically for the new capabilities above, especially:

- CLI socket lifecycle,
- OpenCode API and SSE behavior,
- context/prompt-cache stability,
- multi-instance path resolution,
- Telegram routing behavior,
- and provider/MCP compatibility edge cases.

That added test surface reflects how much the fork has moved beyond upstream's default shape.

## 16. Backports and Hardening That Are Not Entirely Fork-Unique

Not every diff in this fork is a brand-new product feature. Some are implementation variants, retained patches, or opinionated integrations layered onto concepts upstream also has.

Examples:

- Telegram routing and allowlist behavior is more owner-centric here, but upstream already has substantial Telegram topic/reply handling.
- Session compaction exists upstream, but the fork adds more token-budget and client-observability logic around it.
- Provider and MCP handling are both hardened upstream already; the fork mainly differs in specific edge-case behaviors and integrations.

So the most reliable way to read this document is: it describes the user-visible and architectural differences that currently exist in this fork, not a claim that every underlying idea originated only here.

## 17. Features the Fork Explicitly Reframes or Replaces

This fork does not just add features; it also changes how some upstream concepts are implemented.

### Replaced or substantially reframed

- **Memory**: upstream two-file memory is extended with a structured `subconscious` + `qmd` layer, and recall/injection behavior changes significantly when that layer is enabled.
- **CLI mode**: no longer just local direct chat; always launches the pi-tui TUI connected to a local or remote gateway.
- **Cross-channel behavior**: sessions can be shared and mirrored instead of being mostly channel-local.
- **Heartbeat**: isolated from memory and given its own execution model path.
- **Client model**: OpenCode attachment turns nanobot into a backend service for an external TUI.
- **Distributed execution**: node mode enables multi-machine operation from a single gateway, with remote shell execution and chat routing over WebSocket.

## 18. Quick Checklist of the Biggest Feature Gaps vs Upstream

If you need the shortest practical summary, the fork currently has these major capabilities that upstream nanobot does not have in the same integrated form:

- subconscious memory with `qmd` semantic retrieval and periodic background nudge reviews,
- `memory_search` recall tool,
- pi-tui TUI with local and remote gateway connectivity,
- shared-session cross-channel mirroring,
- OpenCode TUI HTTP+SSE backend,
- OpenCode-compatible permissions/revert/fork/summarize flows,
- richer context/token reporting to clients,
- isolated heartbeat model and routing behavior,
- Pi subagent delegation,
- stronger MCP transport support,
- more defensive provider compatibility handling,
- distributed node mode with WebSocket-based remote execution and node-as-channel,
- secret redaction pipeline for all tool output (API keys, DB URIs, auth headers, env secrets, private keys),
- parallel tool execution for read-only and network tools with configurable concurrency,
- context references (`@file:`, `@url:`, `@diff`, `@staged`, `@git:N`) with security deny-list,
- iterative context compression with structured summaries and running summary updates,
- and multi-instance config/workspace path support.

## 19. Secret Redaction Pipeline

Primary files:

- `nanobot/security/redact.py`
- `nanobot/agent/tools/registry.py`
- `nanobot/agent/context.py`

### What changed

This fork adds a regex-based secret redaction pipeline that scans all tool output for credentials before they re-enter model context.

### Pattern coverage

The pipeline detects and replaces:

- API keys: OpenAI (`sk-`), GitHub (`ghp_`), Google (`AIza`), Groq (`gsk_`), xAI (`xai-`), AWS (`AKIA`), GitLab (`glpat-`), NPM (`npm_`), PyPI (`pypi-`)
- Telegram bot tokens
- Database connection strings (PostgreSQL, MySQL, MongoDB, Redis)
- Authorization headers (Bearer, Basic, Token)
- JSON credential fields (password, api_key, secret, token, credential, auth)
- Environment variable assignments containing SECRET, KEY, TOKEN, PASSWORD, or CREDENTIAL
- Private key blocks (RSA, EC, OPENSSH, PGP, DSA)

Each match is replaced with `[REDACTED:label]` to indicate what was redacted.

### Dual enforcement

Secret redaction runs at two layers for defense-in-depth:

1. `ToolRegistry.execute()` — immediately after any tool returns its result
2. `ContextBuilder.add_tool_result()` — before appending to the message list

This ensures secrets from tool output never reach the model context regardless of the code path.

## 20. Parallel Tool Execution

Primary files:

- `nanobot/agent/tools/base.py`
- `nanobot/agent/loop.py`
- `nanobot/config/schema.py`

### What changed

The fork adds a `parallel_safe` property to the base `Tool` class and replaces the sequential tool execution loop with batched parallel execution.

### How it works

- Each tool declares `parallel_safe = True` if it can safely run concurrently
- Tools marked parallel-safe: `read_file`, `list_dir`, `web_search`, `web_fetch`, `memory_search`
- When the agent loop processes tool calls, parallel-safe tools that don't need permission approval are batched and executed via `asyncio.gather`
- A configurable semaphore (`tools.maxParallelTools`, default 8) limits maximum concurrency
- Permission-requiring tools and write tools always execute sequentially
- Results are collected in original tool-call order (gather preserves order)
- Progress events (tool_start/tool_done) are still emitted per tool

### Why this matters

When the model requests multiple read-only operations in one turn (e.g. reading 5 files), they execute concurrently instead of sequentially. This reduces turn latency proportionally to the number of parallel-safe tools requested.

## 21. Context References

Primary files:

- `nanobot/agent/references.py`
- `nanobot/agent/context.py`

### What changed

This fork adds `@-reference` expansion in user messages, letting users inline file content, git state, and web content directly into their prompt.

### Supported references

| Pattern | Handler |
|---------|---------|
| `@file:path` | Read file relative to workspace |
| `@folder:path/` | List directory (depth 1) |
| `@url:https://...` | Fetch URL (10s timeout, 100KB max) |
| `@diff` | `git diff` in workspace |
| `@staged` | `git diff --staged` |
| `@git:N` | `git log -N --oneline` (default 10, max 50) |

### Security

- Paths with `..` traversal outside workspace are blocked
- Deny list: `.ssh/`, `.gnupg/`, `.env`, `*credentials*`, `*.pem`, `*_rsa`, `*_key`
- No `file://` or `ftp://` URLs
- All expanded content passes through `redact_secrets()`
- Token budget: max 50% of `context_tokens`, estimated as `len//4`

## 22. Background Memory Nudge

Primary files:

- `nanobot/agent/subconscious.py`
- `nanobot/agent/loop.py`
- `nanobot/config/schema.py`

### What changed

The fork adds a periodic big-picture memory review that fires every N turns (configurable via `tools.subconscious.nudgeInterval`, default 10).

### How it works

- A counter increments after each `_save_turn()` call in the agent loop
- When the counter reaches the nudge interval, a background `asyncio.create_task` fires `nudge_review()` with a snapshot of the full session
- The nudge review uses the same extraction pipeline but with a broader prompt emphasizing patterns, recurring themes, evolving preferences, and cross-turn connections that incremental extraction misses
- The counter resets on organic extraction flushes (so active extraction conversations don't also trigger nudges)
- The nudge runs in the background and does not block the user response

### Why this matters

Incremental extraction processes messages in small batches and may miss patterns that only emerge across a full conversation. The nudge review catches these cross-turn connections periodically.

## 23. Iterative Context Compression

Primary files:

- `nanobot/agent/loop.py`

### What changed

The fork improves the existing consolidation/compaction flow with three changes:

1. **Tool output pruning**: Before building the conversation string for summarization, tool messages longer than 200 chars are truncated. This prevents large tool outputs from dominating the summary.

2. **Structured summary template**: The generic "summarize comprehensively" prompt is replaced with a structured template with sections for Goal, Progress, Decisions, Files Modified, Next Steps, and Critical Context.

3. **Iterative update on subsequent compactions**: The running summary is stored in `session.metadata["running_summary"]`. On subsequent compactions, if a previous summary exists, the LLM is asked to merge new messages into the existing summary rather than regenerating from scratch. This preserves information across multiple compaction cycles.

## Reference: Main Fork-Changed Files

The largest code-level divergences from upstream are concentrated in:

- `nanobot/agent/loop.py`
- `nanobot/channels/opencode.py`
- `nanobot/cli/commands.py`
- `nanobot/agent/subconscious.py`
- `nanobot/channels/telegram.py`
- `nanobot/channels/cli_socket.py`
- `nanobot/config/schema.py`
- `nanobot/providers/litellm_provider.py`
- `nanobot/nodes/` (registry, gateway_ws, channel, client)
- `nanobot/agent/tools/remote_exec.py`
- `nanobot/security/redact.py`
- `nanobot/agent/references.py`

Supporting differences are spread across session management, filesystem tools, tests, and developer environment files.
