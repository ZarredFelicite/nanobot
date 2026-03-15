<script lang="ts">
  interface Props {
    value: string;
    disabled: boolean;
    onInput: (value: string) => void;
    onSend: () => void;
  }

  let { value, disabled, onInput, onSend }: Props = $props();
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
  <button onclick={onSend} disabled={disabled || !value.trim()} aria-label="Send message">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z" />
    </svg>
  </button>
</div>

<style>
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
</style>
