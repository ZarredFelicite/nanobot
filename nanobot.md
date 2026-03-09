## High-Impact OpenClaw Features to Implement in Nanobot

Context: single-user setup, prioritizing hackability without sacrificing core capability.

1. **Context Budget Visibility (`/context` + richer `/status`)**
- Show system prompt size, injected file sizes, tool schema overhead, and session token usage.
- Why: quickly explains quality drops and cost spikes.

2. **Manual + Auto Compaction (`/compact`, persisted summaries)**
- Summarize older history into durable compact entries while keeping recent turns intact.
- Why: preserves long-session quality and continuity.

3. **Pre-Compaction Memory Flush (silent durability pass)**
- Before compaction, run a silent memory-write reminder turn.
- Why: reduces loss of durable facts at compaction boundaries.

4. **Session Pruning for Tool Output (TTL-aware)**
- Trim/clear stale bulky tool results in-memory before model calls.
- Why: controls context bloat and reduces prompt waste/cost.

5. **Queue Modes + Per-Session Concurrency Controls**
- Add modes like `collect`, `steer`, `followup`, with debounce + overflow behavior.
- Why: improves responsiveness and reliability under bursty input.

6. **Model Failover + Credential/Profile Rotation**
- Rotate credentials on transient failures with cooldowns, then fallback model chain.
- Why: keeps sessions alive through provider/key/rate-limit issues.

7. **Usage/Cost Telemetry in Chat (`/usage`, `/status`)**
- Per-response token/cost footer and provider usage snapshot.
- Why: immediate cost visibility while iterating.

8. **Heartbeat v2 (active hours, routing, ACK policy)**
- Add active-hour windows, destination routing, and ACK suppression rules.
- Why: turns heartbeat into reliable low-noise automation.

9. **Isolated Cron Jobs with Delivery Modes**
- Support main vs isolated runs and delivery modes (`announce`/`webhook`/`none`) with retries and run logs.
- Why: enables deterministic scheduled work without polluting main context.

10. **Tool Loop Detection / Circuit Breaker**
- Detect repetitive no-progress tool loops and apply guardrails.
- Why: prevents runaway token burn and stalled workflows.

---

### Suggested first 3 to ship
1. Context budget visibility
2. Compaction (manual + auto)
3. Session pruning for tool outputs

These three give the biggest immediate quality + cost improvement with relatively low implementation risk.
