<script lang="ts">
  import type { SubconsciousEvent } from '$lib/types';

  interface Props {
    events: SubconsciousEvent[];
  }

  let { events }: Props = $props();

  const MAX_VISIBLE = 30;

  const visible = $derived(events.slice(-MAX_VISIBLE));

  function icon(action: string): string {
    switch (action) {
      case 'extraction': return '';
      case 'nudge': return '';
      case 'recall': return '';
      case 'classifier': return '';
      case 'consolidation': return '';
      default: return '';
    }
  }

  function nameList(names: string[] | undefined): string {
    if (!names || names.length === 0) return '';
    return names.join(', ');
  }

  function label(e: SubconsciousEvent): string {
    const names = nameList(e.names);
    switch (e.action) {
      case 'extraction':
        return names ? `Extracted: ${names}` : 'Extraction (no notes)';
      case 'nudge':
        return names ? `Nudge: ${names}` : 'Nudge (no notes)';
      case 'recall':
        return names ? `Recalled: ${names}` : `Recalled ${e.results ?? 0} memories`;
      case 'classifier':
        return e.inject ? 'Inject: yes' : 'Inject: skip';
      case 'consolidation':
        return `Compacted ${e.messages ?? 0} msgs, kept ${e.kept ?? 0}`;
      default:
        return e.action;
    }
  }

  function timeStr(ts: number): string {
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
</script>

<div class="subconscious-panel">
  <div class="panel-header">
    <span class="panel-icon">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10" />
        <path d="M12 16v-4M12 8h.01" />
      </svg>
    </span>
    <span class="panel-title">Subconscious</span>
    {#if events.length > 0}
      <span class="event-count">{events.length}</span>
    {/if}
  </div>

  {#if visible.length === 0}
    <div class="empty">No activity yet</div>
  {:else}
    <div class="event-list">
      {#each visible as event, i (event.ts + '-' + i)}
        <div class="event-row" class:fade-in={i === visible.length - 1}>
          <span class="event-icon" class:positive={event.action === 'recall' || (event.action === 'classifier' && event.inject)}
                class:neutral={event.action === 'extraction' || event.action === 'nudge'}
                class:muted={event.action === 'consolidation' || (event.action === 'classifier' && !event.inject)}>
            {icon(event.action)}
          </span>
          <span class="event-label" title={label(event)}>{label(event)}</span>
          <span class="event-time">{timeStr(event.ts)}</span>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .subconscious-panel {
    border-top: 1px solid var(--border);
    padding-top: 0.6rem;
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    min-height: 0;
    max-height: 200px;
  }

  .panel-header {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0 0.1rem;
  }

  .panel-icon {
    color: var(--muted);
    display: flex;
    align-items: center;
    opacity: 0.7;
  }

  .panel-title {
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--muted);
  }

  .event-count {
    font-size: 0.6rem;
    color: var(--muted);
    background: var(--surface);
    border-radius: 999px;
    padding: 0.05rem 0.35rem;
    margin-left: auto;
    opacity: 0.7;
  }

  .empty {
    font-size: 0.72rem;
    color: var(--muted);
    opacity: 0.5;
    padding: 0.25rem 0.1rem;
  }

  .event-list {
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 1px;
  }

  .event-row {
    display: flex;
    align-items: center;
    gap: 0.35rem;
    padding: 0.15rem 0.1rem;
    border-radius: 0.25rem;
    transition: background 150ms;
  }

  .event-row:hover {
    background: var(--surface-hover);
  }

  .event-icon {
    font-size: 0.7rem;
    flex-shrink: 0;
    width: 1.1rem;
    text-align: center;
  }

  .event-icon.positive { opacity: 1; }
  .event-icon.neutral { opacity: 0.8; }
  .event-icon.muted { opacity: 0.5; }

  .event-label {
    font-size: 0.72rem;
    color: var(--text);
    opacity: 0.85;
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .event-time {
    font-size: 0.62rem;
    color: var(--muted);
    opacity: 0.5;
    flex-shrink: 0;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  .fade-in {
    animation: fadeSlide 300ms ease-out;
  }

  @keyframes fadeSlide {
    from {
      opacity: 0;
      transform: translateY(4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
</style>
