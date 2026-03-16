<script lang="ts">
  import type { MessageWithParts, TextPart, ToolPart } from '$lib/types';
  import { formatTimestamp, renderMarkdown, formatTokens, formatCost } from '$lib/utils';

  interface Props {
    message: MessageWithParts;
  }

  let { message }: Props = $props();

  let collapsedTools = $state<Set<string>>(new Set());
  let copied = $state(false);

  function copyMessage(): void {
    const text = textParts.filter(p => p.phase !== 'thinking').map(p => p.text).join('\n\n');
    navigator.clipboard.writeText(text).then(() => {
      copied = true;
      setTimeout(() => { copied = false; }, 1500);
    });
  }

  const textParts = $derived(
    message.parts.filter((part): part is TextPart => part.type === 'text')
  );

  const toolParts = $derived(
    message.parts.filter((part): part is ToolPart => part.type === 'tool')
  );

  const isAssistant = $derived(message.info.role === 'assistant');
  const isUser = $derived(message.info.role === 'user');

  const thinkingParts = $derived(textParts.filter(p => p.phase === 'thinking'));
  const contentParts = $derived(textParts.filter(p => p.phase !== 'thinking'));

  function toggleTool(id: string): void {
    const next = new Set(collapsedTools);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    collapsedTools = next;
  }

  function isCollapsed(id: string): boolean {
    return collapsedTools.has(id);
  }

  function toolStatusIcon(status: string): string {
    switch (status) {
      case 'completed': return '✓';
      case 'error': return '✗';
      default: return '⋯';
    }
  }
</script>

<article class:assistant={isAssistant} class:user={isUser} class="msg">
  {#if isUser}
    <div class="user-content">
      {#each textParts as part (part.id)}
        <div class="user-text">{part.text}</div>
      {/each}
    </div>
  {:else}
    <div class="assistant-header">
      {#if message.info.modelID}
        <span class="model-tag">{message.info.modelID}</span>
      {/if}
      <time>{formatTimestamp(message.info.time.created)}</time>
      {#if message.info.tokens}
        <span class="token-info">{formatTokens(message.info.tokens)}</span>
      {/if}
      {#if message.info.cost}
        <span class="cost-info">{formatCost(message.info.cost)}</span>
      {/if}
      <button class="copy-btn" onclick={copyMessage} aria-label="Copy message">
        {#if copied}
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 6L9 17l-5-5" />
          </svg>
        {:else}
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
            <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
          </svg>
        {/if}
      </button>
    </div>

    <div class="content-stack">
      {#if textParts.length === 0 && toolParts.length === 0}
        <p class="placeholder">Generating...</p>
      {/if}

      {#if thinkingParts.length > 0}
        <details class="thinking-block">
          <summary>Thinking</summary>
          <div class="thinking-content">
            {#each thinkingParts as part (part.id)}
              <pre>{part.text}</pre>
            {/each}
          </div>
        </details>
      {/if}

      {#each contentParts as part (part.id)}
        <div class="text-part">
          {@html renderMarkdown(part.text)}
        </div>
      {/each}

      {#if toolParts.length > 0}
        <div class="tool-group">
          {#each toolParts as part (part.id)}
            <div class="tool-call" class:error={part.state.status === 'error'}>
              <button class="tool-header" onclick={() => toggleTool(part.id)}>
                <span class="tool-status" class:running={part.state.status === 'running'} class:completed={part.state.status === 'completed'} class:error={part.state.status === 'error'}>
                  {toolStatusIcon(part.state.status)}
                </span>
                <span class="tool-name">{part.tool}</span>
                {#if part.state.title}
                  <span class="tool-title">{part.state.title}</span>
                {/if}
                <svg class="chevron" class:open={!isCollapsed(part.id)} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                  <path d="M6 9l6 6 6-6" />
                </svg>
              </button>

              {#if !isCollapsed(part.id)}
                <div class="tool-body">
                  {#if Object.keys(part.state.input ?? {}).length}
                    <div class="tool-params">
                      {#each Object.entries(part.state.input) as [key, value]}
                        <div class="param">
                          <span class="param-key">{key}</span>
                          <pre class="param-value">{String(value)}</pre>
                        </div>
                      {/each}
                    </div>
                  {/if}

                  {#if part.state.output}
                    <pre class="tool-output">{part.state.output}</pre>
                  {:else if part.state.error}
                    <pre class="tool-output tool-error">{part.state.error}</pre>
                  {/if}
                </div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</article>

<style>
  .msg {
    max-width: 100%;
    min-width: 0;
  }

  .msg.user {
    display: flex;
    justify-content: flex-end;
  }

  .user-content {
    max-width: 75%;
    background: rgba(96, 181, 240, 0.12);
    border: 1px solid rgba(96, 181, 240, 0.15);
    border-radius: 1rem 1rem 0.25rem 1rem;
    padding: 0.7rem 1rem;
  }

  .user-text {
    white-space: pre-wrap;
    word-break: break-word;
    line-height: 1.5;
    font-size: 0.9rem;
  }

  .msg.assistant {
    padding: 0.25rem 0;
  }

  .assistant-header {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.4rem;
    flex-wrap: wrap;
  }

  .model-tag {
    font-size: 0.7rem;
    font-weight: 500;
    color: var(--accent);
    background: rgba(110, 231, 168, 0.08);
    border: 1px solid rgba(110, 231, 168, 0.12);
    padding: 0.15rem 0.45rem;
    border-radius: 0.35rem;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  time, .token-info, .cost-info {
    font-size: 0.7rem;
    color: var(--muted);
  }

  .copy-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: none;
    color: var(--muted);
    cursor: pointer;
    padding: 0.15rem;
    border-radius: 0.25rem;
    opacity: 0;
    transition: opacity 150ms, color 150ms;
  }

  .msg.assistant:hover .copy-btn {
    opacity: 1;
  }

  .copy-btn:hover {
    color: var(--accent);
  }

  .content-stack {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .text-part {
    line-height: 1.65;
    font-size: 0.9rem;
  }

  .text-part :global(p) {
    margin: 0 0 0.5rem;
  }

  .text-part :global(p:last-child) {
    margin-bottom: 0;
  }

  .text-part :global(pre) {
    background: rgba(0, 0, 0, 0.3);
    border: 1px solid var(--border);
    border-radius: 0.5rem;
    padding: 0.75rem 1rem;
    overflow-x: auto;
    font-size: 0.82rem;
    line-height: 1.5;
    margin: 0.5rem 0;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }

  .text-part :global(code) {
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.85em;
  }

  .text-part :global(:not(pre) > code) {
    background: rgba(255, 255, 255, 0.06);
    padding: 0.15rem 0.35rem;
    border-radius: 0.25rem;
    font-size: 0.82em;
  }

  .text-part :global(ul), .text-part :global(ol) {
    margin: 0.3rem 0;
    padding-left: 1.5rem;
  }

  .text-part :global(li) {
    margin-bottom: 0.2rem;
  }

  .text-part :global(blockquote) {
    margin: 0.5rem 0;
    padding-left: 0.75rem;
    border-left: 3px solid var(--border);
    color: var(--muted);
  }

  .text-part :global(h1), .text-part :global(h2), .text-part :global(h3) {
    margin: 0.75rem 0 0.35rem;
    font-size: 1rem;
    font-weight: 600;
  }

  .text-part :global(a) {
    color: var(--accent-2);
    text-decoration: none;
  }

  .text-part :global(a:hover) {
    text-decoration: underline;
  }

  .thinking-block {
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 0.5rem;
    background: rgba(255, 255, 255, 0.02);
    overflow: hidden;
  }

  .thinking-block summary {
    padding: 0.4rem 0.7rem;
    font-size: 0.75rem;
    color: var(--muted);
    cursor: pointer;
    user-select: none;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .thinking-content {
    padding: 0 0.7rem 0.5rem;
    opacity: 0.6;
  }

  .thinking-content pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.78rem;
    line-height: 1.5;
  }

  .tool-group {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .tool-call {
    border: 1px solid rgba(255, 255, 255, 0.05);
    border-radius: 0.5rem;
    background: rgba(0, 0, 0, 0.15);
    overflow: hidden;
  }

  .tool-call.error {
    border-color: rgba(248, 113, 113, 0.15);
  }

  .tool-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
    padding: 0.45rem 0.65rem;
    background: none;
    border: none;
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .tool-header:hover {
    background: rgba(255, 255, 255, 0.02);
  }

  .tool-status {
    font-size: 0.75rem;
    font-weight: 600;
    flex-shrink: 0;
    width: 1.1rem;
    text-align: center;
  }

  .tool-status.running {
    color: #fbbf24;
  }

  .tool-status.completed {
    color: var(--accent);
  }

  .tool-status.error {
    color: var(--danger);
  }

  .tool-name {
    font-size: 0.78rem;
    font-weight: 500;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    color: var(--text);
  }

  .tool-title {
    font-size: 0.75rem;
    color: var(--muted);
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .chevron {
    margin-left: auto;
    flex-shrink: 0;
    color: var(--muted);
    transition: transform 150ms;
  }

  .chevron.open {
    transform: rotate(180deg);
  }

  .tool-body {
    padding: 0 0.65rem 0.5rem;
    border-top: 1px solid rgba(255, 255, 255, 0.04);
  }

  .tool-params {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    padding-top: 0.4rem;
  }

  .param {
    display: flex;
    flex-direction: column;
    gap: 0.1rem;
  }

  .param-key {
    font-size: 0.68rem;
    text-transform: uppercase;
    color: var(--muted);
    letter-spacing: 0.05em;
    font-weight: 500;
  }

  .param-value {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.78rem;
    line-height: 1.4;
    max-height: 200px;
    overflow-y: auto;
  }

  .tool-output {
    margin: 0.35rem 0 0;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.78rem;
    line-height: 1.4;
    max-height: 300px;
    overflow-y: auto;
    color: var(--muted);
  }

  .tool-error {
    color: var(--danger);
  }

  .placeholder {
    margin: 0;
    color: var(--muted);
    font-size: 0.85rem;
    font-style: italic;
  }
</style>
