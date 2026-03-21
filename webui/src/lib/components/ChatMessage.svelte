<script lang="ts">
  import type { MessageWithParts, TextPart, ToolPart } from '$lib/types';
  import { formatTimestamp, renderMarkdown, formatTokens, formatCost } from '$lib/utils';

  interface Props {
    message: MessageWithParts;
  }

  let { message }: Props = $props();

  // Track expanded tools — edit/write tools with diffs auto-expand
  let expandedTools = $state<Set<string>>(new Set());
  let autoExpandedIds = $state<Set<string>>(new Set());

  // Auto-expand tools that have diff metadata
  $effect(() => {
    for (const part of message.parts) {
      if (part.type === 'tool' && !autoExpandedIds.has(part.id) && part.state.metadata?.filediff) {
        autoExpandedIds = new Set([...autoExpandedIds, part.id]);
        expandedTools = new Set([...expandedTools, part.id]);
      }
    }
  });
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

  const isAssistant = $derived(message.info.role === 'assistant');
  const isUser = $derived(message.info.role === 'user');
  const isCompact = $derived(message.info.mode === 'compact');

  const contentParts = $derived(textParts.filter(p => p.phase !== 'thinking'));

  function toggleTool(id: string): void {
    const next = new Set(expandedTools);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    expandedTools = next;
  }

  function isExpanded(id: string): boolean {
    return expandedTools.has(id);
  }

  function toolStatusIcon(status: string): string {
    switch (status) {
      case 'completed': return '✓';
      case 'error': return '✗';
      default: return '⋯';
    }
  }

  function toolSummary(part: ToolPart): string {
    return part.state.title || '';
  }

  function toolCommand(part: ToolPart): string {
    const input = part.state.input ?? {};
    return input.command || input.filePath || input.pattern || input.query || input.url || '';
  }

  function toolOutputLineCount(part: ToolPart): number {
    if (!part.state.output) return 0;
    return part.state.output.split('\n').filter((l: string) => l.trim()).length;
  }

  function wordCount(text: string): number {
    return text.trim().split(/\s+/).length;
  }

  interface FileDiff {
    file: string;
    before: string;
    after: string;
    additions: number;
    deletions: number;
  }

  interface DiffHunk {
    header: string;
    lines: { type: 'add' | 'del' | 'ctx'; num: number | null; text: string }[];
  }

  function getFileDiff(part: ToolPart): FileDiff | null {
    const meta = part.state.metadata;
    if (!meta || !meta.filediff) return null;
    return meta.filediff as FileDiff;
  }

  function parseDiffHunks(part: ToolPart): DiffHunk[] {
    const meta = part.state.metadata;
    if (!meta || typeof meta.diff !== 'string') return [];
    const raw = meta.diff as string;
    const hunks: DiffHunk[] = [];
    let current: DiffHunk | null = null;
    let addLine = 0;
    let delLine = 0;

    for (const line of raw.split('\n')) {
      if (line.startsWith('@@')) {
        const match = line.match(/@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
        current = { header: line, lines: [] };
        hunks.push(current);
        delLine = match ? parseInt(match[1]) : 0;
        addLine = match ? parseInt(match[2]) : 0;
      } else if (current) {
        if (line.startsWith('+')) {
          current.lines.push({ type: 'add', num: addLine++, text: line.slice(1) });
        } else if (line.startsWith('-')) {
          current.lines.push({ type: 'del', num: delLine++, text: line.slice(1) });
        } else if (!line.startsWith('\\')) {
          current.lines.push({ type: 'ctx', num: addLine, text: line.startsWith(' ') ? line.slice(1) : line });
          addLine++;
          delLine++;
        }
      }
    }
    return hunks;
  }

  function hasDiff(part: ToolPart): boolean {
    return !!(part.state.metadata?.filediff);
  }

  function diffSummary(part: ToolPart): string {
    const fd = getFileDiff(part);
    if (!fd) return '';
    const parts: string[] = [];
    if (fd.additions > 0) parts.push(`+${fd.additions}`);
    if (fd.deletions > 0) parts.push(`-${fd.deletions}`);
    return parts.join(' ');
  }
</script>

<article class:assistant={isAssistant} class:user={isUser} class:compact={isCompact} class="msg">
  {#if isCompact}
    <div class="compact-divider">
      <div class="compact-line"></div>
      <span class="compact-label">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 002 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0022 16z" />
          <path d="M7.5 4.21l4.5 2.6 4.5-2.6M7.5 19.79V14.6L3 12M21 12l-4.5 2.6v5.19" />
        </svg>
        {#each contentParts as part (part.id)}
          {part.text}
        {/each}
      </span>
      <div class="compact-line"></div>
    </div>
  {:else if isUser}
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
      {#if message.parts.length === 0}
        <p class="placeholder">Generating...</p>
      {/if}

      {#each message.parts as part (part.id)}
        {#if part.type === 'text' && part.phase === 'thinking'}
          {#if wordCount(part.text) > 30}
            <details class="thinking-block">
              <summary>Thinking</summary>
              <div class="thinking-content">{part.text}</div>
            </details>
          {:else}
            <div class="thinking-inline">{part.text}</div>
          {/if}
        {:else if part.type === 'text'}
          <div class="text-part">
            {@html renderMarkdown(part.text)}
          </div>
        {:else if part.type === 'tool'}
          <div class="tool-call" class:error={part.state.status === 'error'}>
            <button class="tool-header" onclick={() => toggleTool(part.id)}>
              <div class="tool-header-lines">
                <div class="tool-header-line1">
                  <span class="tool-status" class:running={part.state.status === 'running'} class:completed={part.state.status === 'completed'} class:error={part.state.status === 'error'}>
                    {toolStatusIcon(part.state.status)}
                  </span>
                  <span class="tool-name">{part.tool}</span>
                  {#if toolSummary(part)}
                    <span class="tool-title">- {toolSummary(part)}</span>
                  {/if}
                  {#if hasDiff(part)}
                    <span class="diff-stats">
                      {#if getFileDiff(part)?.additions}<span class="diff-add">+{getFileDiff(part)?.additions}</span>{/if}
                      {#if getFileDiff(part)?.deletions}<span class="diff-del">-{getFileDiff(part)?.deletions}</span>{/if}
                    </span>
                  {:else if toolOutputLineCount(part) > 0}
                    <span class="tool-lines">[{toolOutputLineCount(part)}]</span>
                  {:else if part.state.status === 'error'}
                    <span class="tool-lines tool-lines-error">[error]</span>
                  {/if}
                  <svg class="chevron" class:open={isExpanded(part.id)} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                    <path d="M6 9l6 6 6-6" />
                  </svg>
                </div>
                {#if toolCommand(part)}
                  <div class="tool-header-line2">{toolCommand(part)}</div>
                {/if}
              </div>
            </button>

            {#if isExpanded(part.id)}
              <div class="tool-body">
                {#if hasDiff(part)}
                  <!-- Diff viewer for edit/write tools -->
                  <div class="diff-viewer">
                    <div class="diff-file-header">
                      <span class="diff-file-path">{getFileDiff(part)?.file}</span>
                      <span class="diff-file-stats">
                        {#if getFileDiff(part)?.additions}<span class="diff-add">+{getFileDiff(part)?.additions}</span>{/if}
                        {#if getFileDiff(part)?.deletions}<span class="diff-del">-{getFileDiff(part)?.deletions}</span>{/if}
                      </span>
                    </div>
                    {#each parseDiffHunks(part) as hunk}
                      <div class="diff-hunk">
                        <div class="diff-hunk-header">{hunk.header}</div>
                        {#each hunk.lines as line}
                          <div class="diff-line" class:diff-line-add={line.type === 'add'} class:diff-line-del={line.type === 'del'} class:diff-line-ctx={line.type === 'ctx'}>
                            <span class="diff-line-num">{line.num ?? ''}</span>
                            <span class="diff-line-marker">{line.type === 'add' ? '+' : line.type === 'del' ? '-' : ' '}</span>
                            <span class="diff-line-text">{line.text}</span>
                          </div>
                        {/each}
                      </div>
                    {/each}
                  </div>
                  {#if part.state.error}
                    <pre class="tool-output tool-error">{part.state.error}</pre>
                  {/if}
                {:else}
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
                {/if}
              </div>
            {/if}
          </div>
        {/if}
      {/each}
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
    border-radius: 0.35rem;
    overflow: hidden;
  }

  .thinking-block summary {
    padding: 0.2rem 0;
    font-size: 0.78rem;
    color: var(--muted);
    opacity: 0.5;
    cursor: pointer;
    user-select: none;
  }

  .thinking-content {
    padding: 0 0 0.3rem;
    font-size: 0.8rem;
    line-height: 1.5;
    color: var(--muted);
    opacity: 0.5;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .thinking-inline {
    font-size: 0.8rem;
    line-height: 1.5;
    color: var(--muted);
    opacity: 0.5;
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
    display: block;
    width: 100%;
    padding: 0.45rem 0.65rem;
    background: none;
    border: none;
    color: inherit;
    cursor: pointer;
    font: inherit;
    text-align: left;
  }

  .tool-header-lines {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .tool-header-line1 {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .tool-header-line2 {
    font-size: 0.72rem;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    color: var(--muted);
    padding-left: 1.6rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
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

  .tool-lines {
    font-size: 0.7rem;
    color: var(--muted);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    flex-shrink: 0;
  }

  .tool-lines-error {
    color: var(--danger);
  }

  .diff-stats {
    display: inline-flex;
    gap: 0.35rem;
    font-size: 0.7rem;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    flex-shrink: 0;
  }

  .diff-add {
    color: #4ade80;
  }

  .diff-del {
    color: #f87171;
  }

  /* Diff viewer */
  .diff-viewer {
    margin-top: 0.4rem;
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 0.4rem;
    overflow: hidden;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    font-size: 0.75rem;
    line-height: 1.55;
  }

  .diff-file-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.35rem 0.65rem;
    background: rgba(255, 255, 255, 0.03);
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
  }

  .diff-file-path {
    color: var(--text);
    font-size: 0.72rem;
    font-weight: 500;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .diff-file-stats {
    display: flex;
    gap: 0.4rem;
    font-size: 0.7rem;
    flex-shrink: 0;
  }

  .diff-hunk {
    border-top: 1px solid rgba(255, 255, 255, 0.04);
  }

  .diff-hunk:first-child {
    border-top: none;
  }

  .diff-hunk-header {
    padding: 0.2rem 0.65rem;
    color: rgba(148, 163, 184, 0.6);
    background: rgba(255, 255, 255, 0.015);
    font-size: 0.68rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .diff-line {
    display: flex;
    white-space: pre;
    min-height: 1.55em;
  }

  .diff-line-num {
    width: 3.2rem;
    flex-shrink: 0;
    text-align: right;
    padding-right: 0.5rem;
    color: rgba(148, 163, 184, 0.3);
    user-select: none;
  }

  .diff-line-marker {
    width: 1.2rem;
    flex-shrink: 0;
    text-align: center;
    user-select: none;
  }

  .diff-line-text {
    flex: 1;
    min-width: 0;
    padding-right: 0.5rem;
  }

  .diff-line-add {
    background: rgba(74, 222, 128, 0.08);
  }

  .diff-line-add .diff-line-marker,
  .diff-line-add .diff-line-text {
    color: #4ade80;
  }

  .diff-line-del {
    background: rgba(248, 113, 113, 0.08);
  }

  .diff-line-del .diff-line-marker,
  .diff-line-del .diff-line-text {
    color: #f87171;
  }

  .diff-line-ctx {
    background: transparent;
  }

  .diff-line-ctx .diff-line-text {
    color: var(--muted);
  }

  .diff-line-ctx .diff-line-marker {
    color: transparent;
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

  .msg.compact {
    padding: 0.5rem 0;
  }

  .compact-divider {
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .compact-line {
    flex: 1;
    height: 1px;
    background: rgba(251, 191, 36, 0.2);
  }

  .compact-label {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.72rem;
    font-weight: 500;
    color: #fbbf24;
    white-space: nowrap;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  .compact-label svg {
    opacity: 0.7;
  }
</style>
