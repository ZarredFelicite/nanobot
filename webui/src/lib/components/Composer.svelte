<script lang="ts">
  import type { ProviderInfo } from '$lib/types';

  interface Props {
    value: string;
    disabled: boolean;
    models: { provider: string; model: string; label: string }[];
    selectedModel: string;
    onInput: (value: string) => void;
    onSend: () => void;
    onModelChange: (model: string) => void;
    onAbort?: () => void;
  }

  let { value, disabled, models, selectedModel, onInput, onSend, onModelChange, onAbort }: Props = $props();
  let textareaEl: HTMLTextAreaElement | undefined = $state();

  function handleKeydown(event: KeyboardEvent): void {
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
</style>
