<script lang="ts">
  import type { ContextBreakdown, SlashCommandInfo } from '$lib/types';

  interface Props {
    value: string;
    disabled: boolean;
    models: { provider: string; model: string; label: string }[];
    selectedModel: string;
    contextInfo?: {
      budget?: number;
      usagePercent?: number;
      final?: { total: number };
      breakdown?: ContextBreakdown;
      withinBudget?: boolean;
      hasCompacted?: boolean;
      totalCompactions?: number;
      lastCompactedMessages?: number;
    } | null;
    slashActive?: boolean;
    slashSuggestions?: SlashCommandInfo[];
    selectedSlashIndex?: number;
    onInput: (value: string) => void;
    onSend: () => void;
    onModelChange: (model: string) => void;
    onAbort?: () => void;
    onSelectSlash?: (index: number) => void;
    onHighlightSlash?: (index: number) => void;
    onCycleSlash?: (direction: 1 | -1) => void;
  }

  let {
    value,
    disabled,
    models,
    selectedModel,
    contextInfo = null,
    slashActive = false,
    slashSuggestions = [],
    selectedSlashIndex = 0,
    onInput,
    onSend,
    onModelChange,
    onAbort,
    onSelectSlash,
    onHighlightSlash,
    onCycleSlash
  }: Props = $props();

  let breakdownSegments = $derived.by(() => {
    const breakdown = contextInfo?.breakdown;
    const budget = contextInfo?.budget ?? 0;
    if (!breakdown || budget <= 0) return [];
    return [
      {
        key: 'systemPrompt',
        label: 'System prompt',
        value: breakdown.systemPrompt,
        color: '#9ccfd8'
      },
      {
        key: 'skills',
        label: 'Skills',
        value: breakdown.skills,
        color: '#c4a7e7'
      },
      {
        key: 'toolOutputs',
        label: 'Tool outputs',
        value: breakdown.toolOutputs,
        color: '#ebbcba'
      },
      {
        key: 'messages',
        label: 'Messages',
        value: breakdown.messages,
        color: '#31748f'
      }
    ].filter((segment) => segment.value > 0).map((segment) => ({
      ...segment,
      width: Math.max((segment.value / budget) * 100, 0.8)
    }));
  });

  let freeWidth = $derived.by(() => {
    const used = breakdownSegments.reduce((sum, segment) => sum + segment.width, 0);
    return Math.max(0, 100 - used);
  });
  let textareaEl: HTMLTextAreaElement | undefined = $state();

  function handleKeydown(event: KeyboardEvent): void {
    if (slashActive && slashSuggestions.length > 0) {
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        onCycleSlash?.(1);
        return;
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        onCycleSlash?.(-1);
        return;
      }
      if (event.key === 'Tab') {
        event.preventDefault();
        onSelectSlash?.(selectedSlashIndex);
        return;
      }
      if (event.key === 'Enter' && !event.shiftKey) {
        event.preventDefault();
        onSend();
        return;
      }
    }

    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      onSend();
    }
  }

  function handleInput(event: Event): void {
    const el = event.currentTarget as HTMLTextAreaElement;
    onInput(el.value);
    autoResize(el);
  }

  function autoResize(el: HTMLTextAreaElement): void {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }

  $effect(() => {
    if (textareaEl && value === '') {
      textareaEl.style.height = 'auto';
    }
  });
</script>

<div class="composer-wrap">
  <div class="model-bar">
    <select
      class="model-select"
      value={selectedModel}
      onchange={(e) => onModelChange((e.currentTarget as HTMLSelectElement).value)}
    >
      {#each models as m (m.label)}
        <option value={m.label}>{m.label}</option>
      {/each}
    </select>

    {#if contextInfo?.usagePercent != null}
      <div class="context-usage" title={`${contextInfo.final?.total ?? 0} / ${contextInfo.budget ?? 0} tokens`}>
        <div class="context-bar" class:over-budget={contextInfo.withinBudget === false}>
          {#each breakdownSegments as segment (segment.key)}
            <div
              class="context-segment"
              style={`width:${segment.width}%; background:${segment.color}`}
              title={`${segment.label}: ${segment.value.toLocaleString()} tokens`}
            ></div>
          {/each}
          {#if freeWidth > 0}
            <div
              class="context-segment context-segment-free"
              style={`width:${freeWidth}%`}
              title={`Free space: ${Math.max(0, (contextInfo.budget ?? 0) - (contextInfo.final?.total ?? 0)).toLocaleString()} tokens`}
            ></div>
          {/if}
        </div>
        <div class="context-meta">
          <span class="context-numbers">{Math.round(contextInfo.usagePercent)}%</span>
          {#if contextInfo.hasCompacted}
            <span
              class="context-compacted"
              title={contextInfo.lastCompactedMessages
                ? `Last compaction consolidated ${contextInfo.lastCompactedMessages} messages.`
                : 'Context was compacted'}
            >
              compacted{contextInfo.totalCompactions && contextInfo.totalCompactions > 1 ? ` ×${contextInfo.totalCompactions}` : ''}
            </span>
          {/if}
        </div>
      </div>
    {/if}
  </div>
  <div class="composer">
    <textarea
      bind:this={textareaEl}
      rows="1"
      placeholder="Message nanobot..."
      {value}
      oninput={handleInput}
      onkeydown={handleKeydown}
      {disabled}
    ></textarea>
    {#if onAbort}
      <button class="abort-btn" onclick={onAbort} aria-label="Stop generation">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
          <rect x="4" y="4" width="16" height="16" rx="2" />
        </svg>
      </button>
    {:else}
      <button onclick={onSend} disabled={disabled || !value.trim()} aria-label="Send message">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
        </svg>
      </button>
    {/if}
  </div>

  {#if slashActive && slashSuggestions.length > 0}
    <div class="slash-menu" role="listbox" aria-label="Slash commands">
      {#each slashSuggestions as command, index (command.name)}
        <button
          type="button"
          class:selected={index === selectedSlashIndex}
          class="slash-item"
          onclick={() => onSelectSlash?.(index)}
          onmouseenter={() => onHighlightSlash?.(index)}
        >
          <div class="slash-item-top">
            <span class="slash-name">/{command.name}</span>
            <span class="slash-template">{command.template}</span>
          </div>
          <div class="slash-description">{command.description}</div>
        </button>
      {/each}
    </div>
  {/if}
</div>

<style>
  .composer-wrap {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .model-bar {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .context-usage {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 0.6rem;
  }

  .context-bar {
    flex: 1;
    min-width: 120px;
    height: 0.55rem;
    border-radius: 999px;
    overflow: hidden;
    display: flex;
    background: rgba(25, 23, 36, 0.9);
    border: 1px solid rgba(110, 106, 134, 0.45);
  }

  .context-bar.over-budget {
    border-color: rgba(235, 188, 186, 0.75);
    box-shadow: 0 0 0 1px rgba(235, 188, 186, 0.25) inset;
  }

  .context-segment {
    height: 100%;
  }

  .context-segment-free {
    background: rgba(110, 106, 134, 0.12);
  }

  .context-meta {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    flex-shrink: 0;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.68rem;
    color: #908caa;
  }

  .context-numbers {
    color: #e0def4;
  }

  .context-compacted {
    color: #f6c177;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    font-size: 0.62rem;
  }

  .model-select {
    appearance: none;
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 0.4rem;
    color: var(--muted);
    font: inherit;
    font-size: 0.72rem;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    padding: 0.25rem 0.5rem;
    cursor: pointer;
    outline: none;
    max-width: 100%;
    transition: color 150ms, border-color 150ms;
    /* custom arrow */
    background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' fill='none'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%237a9a8e' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
    background-repeat: no-repeat;
    background-position: right 0.4rem center;
    padding-right: 1.4rem;
  }

  .model-select:hover,
  .model-select:focus {
    color: var(--text);
    border-color: var(--border-hover);
  }

  .model-select option {
    background: #0c141c;
    color: var(--text);
  }

  .composer {
    display: flex;
    align-items: flex-end;
    gap: 0.5rem;
    background: rgba(0, 0, 0, 0.2);
    border: 1px solid var(--border);
    border-radius: 0.75rem;
    padding: 0.5rem;
    transition: border-color 150ms;
  }

  .slash-menu {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    margin-top: 0.1rem;
    padding: 0.35rem;
    border: 1px solid var(--border);
    border-radius: 0.75rem;
    background: rgba(8, 12, 18, 0.95);
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.35);
  }

  .slash-item {
    width: 100%;
    text-align: left;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 0.6rem;
    padding: 0.55rem 0.65rem;
    color: var(--text);
    display: flex;
    flex-direction: column;
    align-items: stretch;
    gap: 0.2rem;
    height: auto;
  }

  .slash-item:hover,
  .slash-item.selected {
    border-color: rgba(110, 231, 168, 0.22);
    background: rgba(110, 231, 168, 0.08);
  }

  .slash-item-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.72rem;
  }

  .slash-name {
    color: #6ee7a8;
    font-weight: 600;
  }

  .slash-template {
    color: var(--muted);
    opacity: 0.8;
  }

  .slash-description {
    color: var(--muted);
    font-size: 0.78rem;
    line-height: 1.35;
  }

  .composer:focus-within {
    border-color: rgba(110, 231, 168, 0.3);
  }

  textarea {
    flex: 1;
    resize: none;
    min-height: 1.5rem;
    max-height: 200px;
    border: none;
    background: transparent;
    color: var(--text);
    padding: 0.35rem 0.5rem;
    font: inherit;
    font-size: 0.9rem;
    line-height: 1.5;
    outline: none;
  }

  textarea::placeholder {
    color: var(--muted);
    opacity: 0.6;
  }

  button {
    flex-shrink: 0;
    width: 2.25rem;
    height: 2.25rem;
    display: flex;
    align-items: center;
    justify-content: center;
    border: none;
    border-radius: 0.5rem;
    background: var(--accent);
    color: #0a1117;
    cursor: pointer;
    transition: opacity 150ms;
  }

  button:disabled {
    opacity: 0.3;
    cursor: not-allowed;
  }

  button:not(:disabled):hover {
    opacity: 0.85;
  }

  .abort-btn {
    flex-shrink: 0;
    width: 2.25rem;
    height: 2.25rem;
    display: flex;
    align-items: center;
    justify-content: center;
    border: none;
    border-radius: 0.5rem;
    background: var(--danger);
    color: #0a1117;
    cursor: pointer;
    transition: opacity 150ms;
  }

  .abort-btn:hover {
    opacity: 0.85;
  }

  @media (max-width: 900px) {
    .model-bar {
      flex-wrap: wrap;
      align-items: flex-start;
    }

    .context-usage {
      width: 100%;
    }
  }
</style>
