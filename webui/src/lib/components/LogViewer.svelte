<script lang="ts">
  import { onMount } from 'svelte';

  interface LogEntry {
    time: string;
    level: string;
    module: string;
    function: string;
    line: number;
    message: string;
  }

  interface Props {
    open: boolean;
    onToggle: () => void;
  }

  let { open, onToggle }: Props = $props();
  let logs: LogEntry[] = $state([]);
  let stream: EventSource | null = null;
  let containerEl: HTMLDivElement | undefined = $state();
  let autoScroll = $state(true);
  let filter = $state('');

  const filtered = $derived(
    filter
      ? logs.filter(
          (l) =>
            l.message.toLowerCase().includes(filter.toLowerCase()) ||
            l.level.toLowerCase().includes(filter.toLowerCase()) ||
            l.module.toLowerCase().includes(filter.toLowerCase())
        )
      : logs
  );

  const levelColor: Record<string, string> = {
    DEBUG: 'var(--muted)',
    INFO: 'var(--accent)',
    WARNING: '#f0a500',
    ERROR: 'var(--danger)',
    CRITICAL: '#ff4444',
  };

  function connect() {
    stream?.close();
    stream = new EventSource('/log/stream');
    stream.onmessage = (event) => {
      try {
        const entry: LogEntry = JSON.parse(event.data);
        logs.push(entry);
        if (logs.length > 1000) {
          logs = logs.slice(-800);
        }
        if (autoScroll && containerEl) {
          requestAnimationFrame(() => {
            containerEl?.scrollTo(0, containerEl.scrollHeight);
          });
        }
      } catch {
        // ignore
      }
    };
    stream.onerror = () => {
      stream?.close();
      setTimeout(connect, 3000);
    };
  }

  function disconnect() {
    stream?.close();
    stream = null;
  }

  function formatTime(iso: string): string {
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString('en-AU', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch {
      return iso.slice(11, 19);
    }
  }

  $effect(() => {
    if (open) {
      connect();
    } else {
      disconnect();
    }
  });

  onMount(() => {
    return () => disconnect();
  });
</script>

{#if open}
  <div class="log-panel">
    <div class="log-header">
      <span class="log-title">Logs</span>
      <input
        class="log-filter"
        type="text"
        placeholder="Filter..."
        bind:value={filter}
      />
      <label class="auto-scroll">
        <input type="checkbox" bind:checked={autoScroll} /> Auto-scroll
      </label>
      <button class="log-clear" onclick={() => (logs = [])}>Clear</button>
      <button class="log-close" onclick={onToggle}>Close</button>
    </div>
    <div class="log-entries" bind:this={containerEl}>
      {#each filtered as entry (entry.time + entry.message)}
        <div class="log-line" style:--level-color={levelColor[entry.level] || 'var(--muted)'}>
          <span class="log-time">{formatTime(entry.time)}</span>
          <span class="log-level">{entry.level.slice(0, 4)}</span>
          <span class="log-source">{entry.module}:{entry.line}</span>
          <span class="log-msg">{entry.message}</span>
        </div>
      {/each}
    </div>
  </div>
{/if}

<style>
  .log-panel {
    display: flex;
    flex-direction: column;
    height: 280px;
    border-top: 1px solid var(--border);
    background: rgba(0, 0, 0, 0.4);
    font-size: 0.75rem;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  .log-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.35rem 0.75rem;
    border-bottom: 1px solid var(--border);
    background: rgba(0, 0, 0, 0.3);
    flex-shrink: 0;
  }

  .log-title {
    font-weight: 600;
    color: var(--text);
    margin-right: auto;
  }

  .log-filter {
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 0.3rem;
    color: var(--text);
    font: inherit;
    padding: 0.15rem 0.4rem;
    width: 10rem;
    outline: none;
  }

  .log-filter:focus {
    border-color: var(--accent);
  }

  .auto-scroll {
    color: var(--muted);
    display: flex;
    align-items: center;
    gap: 0.25rem;
    cursor: pointer;
    white-space: nowrap;
  }

  .auto-scroll input {
    margin: 0;
  }

  .log-clear,
  .log-close {
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 0.3rem;
    color: var(--muted);
    font: inherit;
    padding: 0.15rem 0.5rem;
    cursor: pointer;
  }

  .log-clear:hover,
  .log-close:hover {
    color: var(--text);
    border-color: var(--border-hover);
  }

  .log-entries {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 0.25rem 0;
  }

  .log-line {
    display: flex;
    gap: 0.5rem;
    padding: 0.1rem 0.75rem;
    line-height: 1.5;
    white-space: nowrap;
  }

  .log-line:hover {
    background: rgba(255, 255, 255, 0.03);
  }

  .log-time {
    color: var(--muted);
    flex-shrink: 0;
  }

  .log-level {
    color: var(--level-color);
    font-weight: 600;
    flex-shrink: 0;
    width: 3ch;
  }

  .log-source {
    color: var(--muted);
    flex-shrink: 0;
    max-width: 20ch;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .log-msg {
    color: var(--text);
    overflow: hidden;
    text-overflow: ellipsis;
  }

  @media (max-width: 768px) {
    .log-panel {
      height: 200px;
    }

    .log-source {
      display: none;
    }

    .auto-scroll {
      display: none;
    }
  }
</style>
