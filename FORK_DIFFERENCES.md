# Fork vs Upstream Nanobot

This document describes the major feature and behavior differences between this fork and upstream `HKUDS/nanobot`.

Scope notes:

- This is a code-level comparison of this fork's `main` branch against `origin/main` as checked in this repo.
- It focuses on product behavior, architecture, and developer-facing capabilities, not every small refactor.
- Some changes in this fork are original features; others are selective backports or hardening patches from newer upstream releases.

## Executive Summary

Compared with upstream nanobot, this fork is much more opinionated around a single-user, hackable, coding-assistant workflow.

The biggest differences are:

1. A new hierarchical "subconscious" memory system backed by local markdown notes and `qmd` semantic search.
2. A Unix-socket CLI client mode so `nanobot agent` can attach to a running gateway instead of always running standalone.
3. Cross-channel session mirroring so CLI, Telegram, and other channels can share one live conversation.
4. A full OpenCode TUI HTTP+SSE backend, including session management, streaming, permissions, revert/unrevert, fork, and context reporting.
5. Stronger heartbeat isolation and delivery rules.
6. Pi subagent integration for delegating larger coding/research tasks to an external coding agent process.
7. OWASP-style prompt-injection hardening for user input, remote content, memory recall, and final output.
8. Extra implementation work around MCP cancellation, provider quirks, Telegram owner routing, active-config data paths, and token-aware session compaction.

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

## 2. CLI Can Attach to a Running Gateway via Unix Socket

Primary files:

- `nanobot/channels/cli_socket.py`
- `nanobot/cli/commands.py`
- `nanobot/channels/manager.py`
- `nanobot/config/schema.py`

### What changed

Upstream CLI usage is primarily a direct local agent invocation. This fork adds a Unix domain socket channel so `nanobot agent` can operate as a thin client for an already-running gateway.

### Fork behavior

- `nanobot agent` checks for a running local gateway socket.
- If the socket is available, the CLI sends messages to the gateway instead of starting a separate standalone agent loop.
- If no gateway is reachable, the CLI falls back to standalone behavior.

### Why this matters

This gives the fork a shared-session model across interfaces. A single gateway can own the real session state while the CLI acts like another frontend.

### Important comparison note

Upstream already supports `--config` and `--workspace` on the CLI. The fork-specific difference is not the existence of those flags; it is that the fork combines them with:

- gateway client mode over a Unix socket,
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

## 14. Test Coverage Added for Fork-Specific Behavior

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

## 15. Backports and Hardening That Are Not Entirely Fork-Unique

Not every diff in this fork is a brand-new product feature. Some are implementation variants, retained patches, or opinionated integrations layered onto concepts upstream also has.

Examples:

- Telegram routing and allowlist behavior is more owner-centric here, but upstream already has substantial Telegram topic/reply handling.
- Session compaction exists upstream, but the fork adds more token-budget and client-observability logic around it.
- Provider and MCP handling are both hardened upstream already; the fork mainly differs in specific edge-case behaviors and integrations.

So the most reliable way to read this document is: it describes the user-visible and architectural differences that currently exist in this fork, not a claim that every underlying idea originated only here.

## 16. Features the Fork Explicitly Reframes or Replaces

This fork does not just add features; it also changes how some upstream concepts are implemented.

### Replaced or substantially reframed

- **Memory**: upstream two-file memory is extended with a structured `subconscious` + `qmd` layer, and recall/injection behavior changes significantly when that layer is enabled.
- **CLI mode**: no longer just local direct chat; can act as a gateway client.
- **Cross-channel behavior**: sessions can be shared and mirrored instead of being mostly channel-local.
- **Heartbeat**: isolated from memory and given its own execution model path.
- **Client model**: OpenCode attachment turns nanobot into a backend service for an external TUI.

## 17. Quick Checklist of the Biggest Feature Gaps vs Upstream

If you need the shortest practical summary, the fork currently has these major capabilities that upstream nanobot does not have in the same integrated form:

- subconscious memory with `qmd` semantic retrieval,
- `memory_search` recall tool,
- Unix-socket CLI gateway client mode,
- shared-session cross-channel mirroring,
- OpenCode TUI HTTP+SSE backend,
- OpenCode-compatible permissions/revert/fork/summarize flows,
- richer context/token reporting to clients,
- isolated heartbeat model and routing behavior,
- Pi subagent delegation,
- stronger MCP transport support,
- more defensive provider compatibility handling,
- and multi-instance config/workspace path support.

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

Supporting differences are spread across session management, filesystem tools, tests, and developer environment files.
