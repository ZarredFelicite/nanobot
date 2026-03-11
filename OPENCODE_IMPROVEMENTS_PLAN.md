# OpenCode Channel Improvements Plan

This plan tracks the next hardening pass for `nanobot/channels/opencode.py`.

## 1) Permission Session Isolation
- Replace global `self._current_session_id` usage with per-request context using `contextvars.ContextVar`.
- Ensure permission prompts always include the originating session ID, even when multiple requests run concurrently.
- Add regression test for concurrent permission requests across two sessions.

## 2) `prompt_async` Body Reuse Bug
- Refactor message send flow into a shared internal helper that accepts parsed payload body.
- Use that helper from both `POST /session/{id}/message` and `POST /session/{id}/prompt_async`.
- Add regression test ensuring `prompt_async` processes the original request body correctly.

## 3) Unified Message ID Allocation
- Introduce a shared ID/index allocator based on projected display messages.
- Use it consistently in standard sends, slash commands, and summarize endpoint events.
- Add tests covering command/summarize ID consistency with `/session/{id}/message` output.

## 4) History Part Ordering Parity
- Align `_messages_to_opencode` part order with live SSE order for tool-call turns.
- Emit tool parts before final text when the turn was tool-first in live stream semantics.
- Add regression test validating part order is stable across reload.

## 5) SSE Fanout Hardening
- Change broadcast to concurrent writes with timeout guards per client.
- Continue dropping stale clients on failures.
- Add test for slow/broken client not blocking other active SSE clients.
