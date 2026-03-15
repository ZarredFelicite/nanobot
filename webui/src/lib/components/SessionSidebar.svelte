<script lang="ts">
  import type { SessionInfo, SessionStatus } from '$lib/types';
  import { relativeTimestamp } from '$lib/utils';

  interface Props {
    sessions: SessionInfo[];
    activeSessionId: string;
    statuses: Record<string, SessionStatus['status']>;
    creating: boolean;
    onCreate: () => void;
    onSelect: (sessionId: string) => void;
  }

  let { sessions, activeSessionId, statuses, creating, onCreate, onSelect }: Props = $props();

  function statusLabel(sessionId: string): string {
    return statuses[sessionId]?.type === 'busy' ? 'Busy' : 'Idle';
  }

  function isBusy(sessionId: string): boolean {
    return statuses[sessionId]?.type === 'busy';
  }
</script>

<nav class="sidebar-shell">
  <div class="sidebar-header">
    <span class="brand">Nanobot</span>
    <button class="create-button" onclick={onCreate} disabled={creating}>
      {#if creating}
        <span class="spinner"></span>
      {:else}
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
          <path d="M12 5v14M5 12h14" />
        </svg>
      {/if}
      New
    </button>
  </div>

  <div class="session-list">
    {#if sessions.length === 0}
      <div class="empty-state">
        <p>No sessions</p>
      </div>
    {:else}
      {#each sessions as session (session.id)}
        <button
          class:active={session.id === activeSessionId}
          class="session-card"
          onclick={() => onSelect(session.id)}
        >
          <div class="card-top">
            <span class="card-title">{session.title || 'Untitled'}</span>
            {#if isBusy(session.id)}
              <span class="dot busy"></span>
            {/if}
          </div>
          <div class="card-meta">
            <span>{relativeTimestamp(session.time.updated)}</span>
          </div>
        </button>
      {/each}
    {/if}
  </div>
</nav>

<style>
  .sidebar-shell {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    height: 100%;
    min-height: 0;
  }

  .sidebar-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px solid var(--border);
  }

  .brand {
    font-size: 0.9rem;
    font-weight: 700;
    letter-spacing: 0.02em;
    color: var(--accent);
  }

  .create-button {
    display: inline-flex;
    align-items: center;
    gap: 0.35rem;
    border: 1px solid var(--border);
    border-radius: 0.5rem;
    padding: 0.35rem 0.6rem;
    background: var(--surface);
    color: var(--text);
    font: inherit;
    font-size: 0.78rem;
    font-weight: 500;
    cursor: pointer;
    transition: background 150ms, border-color 150ms;
  }

  .create-button:hover {
    background: var(--surface-hover);
    border-color: var(--border-hover);
  }

  .create-button:disabled {
    opacity: 0.5;
    cursor: wait;
  }

  .spinner {
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
  }

  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  .session-list {
    overflow-y: auto;
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .session-card {
    display: block;
    width: 100%;
    border: 1px solid transparent;
    background: transparent;
    border-radius: 0.5rem;
    padding: 0.6rem 0.65rem;
    text-align: left;
    color: inherit;
    cursor: pointer;
    transition: background 120ms, border-color 120ms;
    font: inherit;
  }

  .session-card:hover {
    background: var(--surface-hover);
  }

  .session-card.active {
    background: rgba(110, 231, 168, 0.08);
    border-color: rgba(110, 231, 168, 0.15);
  }

  .card-top {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.2rem;
  }

  .card-title {
    font-size: 0.82rem;
    font-weight: 500;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1;
    min-width: 0;
  }

  .dot {
    flex-shrink: 0;
    width: 6px;
    height: 6px;
    border-radius: 50%;
  }

  .dot.busy {
    background: var(--accent);
    animation: pulse-anim 1.4s ease-in-out infinite;
  }

  @keyframes pulse-anim {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  .card-meta {
    display: flex;
    gap: 0.5rem;
  }

  .card-meta span {
    font-size: 0.7rem;
    color: var(--muted);
  }

  .empty-state {
    padding: 1rem 0.5rem;
    color: var(--muted);
    font-size: 0.85rem;
    text-align: center;
  }

  .empty-state p {
    margin: 0;
  }
</style>
