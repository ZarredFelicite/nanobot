<script lang="ts">
  import { onMount, tick } from 'svelte';
  import { goto } from '$app/navigation';
  import { page } from '$app/state';
  import Composer from '$lib/components/Composer.svelte';
  import ChatMessage from '$lib/components/ChatMessage.svelte';
  import SessionSidebar from '$lib/components/SessionSidebar.svelte';
  import LogViewer from '$lib/components/LogViewer.svelte';
  import { abortSession, createSession, deleteSession, getMessages, getProviders, getStatuses, listSessions, patchSession, sendMessage } from '$lib/api';
  import type { MessageInfo, MessagePart, MessageWithParts, ProviderInfo, SessionInfo, SessionStatus, SseEvent } from '$lib/types';

  let sessions = $state<SessionInfo[]>([]);
  let statuses = $state<Record<string, SessionStatus['status']>>({});
  let messages = $state<MessageWithParts[]>([]);
  let selectedSessionId = $state('');
  let draft = $state('');
  let sessionsLoading = $state(true);
  let messagesLoading = $state(false);
  let creating = $state(false);
  let sending = $state(false);
  let error = $state('');
  let sidebarOpen = $state(typeof window !== 'undefined' ? window.innerWidth > 768 : true);
  let showScrollButton = $state(false);
  let availableModels = $state<{ provider: string; model: string; label: string }[]>([]);
  let selectedModel = $state('');
  let sseDisconnected = $state(false);
  let editingTitle = $state(false);
  let titleDraft = $state('');
  let logsOpen = $state(false);

  let stream: EventSource | null = null;
  let chatLogEl: HTMLElement | undefined = $state();
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let reconnectDelay = 1000;

  const currentSession = $derived(
    sessions.find((session) => session.id === selectedSessionId) ?? null
  );

  const currentStatus = $derived(
    selectedSessionId ? statuses[selectedSessionId] ?? { type: 'idle' as const } : null
  );

  const isBusy = $derived(currentStatus?.type === 'busy');

  const contextInfo = $derived(
    selectedSessionId ? statuses[selectedSessionId]?.context : null
  );

  onMount(() => {
    void boot();
    connectStream();

    return () => {
      stream?.close();
      if (reconnectTimer) clearTimeout(reconnectTimer);
    };
  });

  function handleGlobalKeydown(event: KeyboardEvent): void {
    const mod = event.ctrlKey || event.metaKey;
    if (!mod) {
      if (event.key === 'Escape') {
        const ta = document.querySelector('textarea') as HTMLElement | null;
        ta?.focus();
        return;
      }
      return;
    }

    const target = event.target as HTMLElement;
    const isInput = target.tagName === 'INPUT' || target.tagName === 'TEXTAREA';

    if (mod && event.key === 'n' && !isInput) {
      event.preventDefault();
      void handleCreateSession();
      return;
    }

    if (mod && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
      event.preventDefault();
      const idx = sessions.findIndex(s => s.id === selectedSessionId);
      if (idx < 0) return;
      const next = event.key === 'ArrowUp'
        ? Math.max(0, idx - 1)
        : Math.min(sessions.length - 1, idx + 1);
      if (next !== idx) void selectSession(sessions[next].id);
    }
  }

  async function boot(): Promise<void> {
    try {
      sessionsLoading = true;
      error = '';

      const [sessionList, statusList, providerList] = await Promise.all([
        listSessions(),
        getStatuses(),
        getProviders(),
      ]);
      sessions = sortSessions(sessionList);
      statuses = Object.fromEntries(
        statusList.map((item: SessionStatus) => [item.sessionID, item.status])
      );

      // Build flat model list from providers
      const models: { provider: string; model: string; label: string }[] = [];
      for (const provider of providerList) {
        for (const modelId of Object.keys(provider.models)) {
          models.push({
            provider: provider.id,
            model: modelId,
            label: `${provider.id}/${modelId}`,
          });
        }
      }
      availableModels = models;
      if (models.length > 0 && !selectedModel) {
        selectedModel = models[0].label;
      }

      const requested = page.url.searchParams.get('session');
      const target = requested && sessions.some((session) => session.id === requested)
        ? requested
        : sessions[0]?.id;

      if (target) {
        await selectSession(target, false);
      } else {
        await handleCreateSession();
      }
    } catch (err) {
      error = toErrorMessage(err, 'Failed to load sessions');
    } finally {
      sessionsLoading = false;
    }
  }

  function connectStream(): void {
    stream?.close();
    stream = new EventSource('/event');

    stream.onmessage = (event) => {
      try {
        applyEvent(JSON.parse(event.data) as SseEvent);
      } catch {
        // Ignore malformed payloads.
      }
    };

    stream.onerror = () => {
      sseDisconnected = true;
      stream?.close();
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(() => {
        connectStream();
      }, reconnectDelay);
      reconnectDelay = Math.min(reconnectDelay * 2, 30000);
    };
  }

  async function handleCreateSession(): Promise<void> {
    try {
      creating = true;
      error = '';
      const session = await createSession();
      upsertSession(session);
      await selectSession(session.id);
    } catch (err) {
      error = toErrorMessage(err, 'Failed to create session');
    } finally {
      creating = false;
    }
  }

  async function selectSession(sessionId: string, updateUrl = true): Promise<void> {
    selectedSessionId = sessionId;
    editingTitle = false;
    error = '';

    // Restore model from session metadata if available
    const session = sessions.find(s => s.id === sessionId);
    if (session?.model && availableModels.some(m => m.label === session.model)) {
      selectedModel = session.model;
    }

    if (updateUrl) {
      const url = new URL(page.url);
      url.searchParams.set('session', sessionId);
      await goto(url, { replaceState: true, noScroll: true, keepFocus: true });
    }

    await loadMessages(sessionId);
  }

  async function loadMessages(sessionId: string): Promise<void> {
    try {
      messagesLoading = true;
      messages = await getMessages(sessionId);
      messages.sort((left, right) => left.info.time.created - right.info.time.created);
      await tick();
      scrollToBottom();
    } catch (err) {
      error = toErrorMessage(err, 'Failed to load messages');
    } finally {
      messagesLoading = false;
    }
  }

  async function handleSend(): Promise<void> {
    const text = draft.trim();
    if (!text || !selectedSessionId || sending) return;

    try {
      sending = true;
      error = '';
      draft = '';
      await sendMessage(selectedSessionId, text, selectedModel || undefined);
      await tick();
      scrollToBottom();
    } catch (err) {
      draft = text;
      error = toErrorMessage(err, 'Failed to send message');
    } finally {
      sending = false;
    }
  }

  async function handleDeleteSession(sessionId: string): Promise<void> {
    try {
      await deleteSession(sessionId);
    } catch (err) {
      error = toErrorMessage(err, 'Failed to delete session');
    }
  }

  async function handleAbort(): Promise<void> {
    if (!selectedSessionId) return;
    try {
      await abortSession(selectedSessionId);
    } catch (err) {
      error = toErrorMessage(err, 'Failed to abort');
    }
  }

  async function handleModelChange(model: string): Promise<void> {
    selectedModel = model;
    if (selectedSessionId) {
      try {
        await patchSession(selectedSessionId, { model });
      } catch {
        // Non-critical — model still used locally
      }
    }
  }

  async function handleTitleSubmit(): Promise<void> {
    if (!selectedSessionId || !editingTitle) return;
    const newTitle = titleDraft.trim();
    editingTitle = false;
    if (!newTitle || newTitle === currentSession?.title) return;
    try {
      await patchSession(selectedSessionId, { title: newTitle });
    } catch (err) {
      error = toErrorMessage(err, 'Failed to rename session');
    }
  }

  function handleTitleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter') {
      event.preventDefault();
      void handleTitleSubmit();
    } else if (event.key === 'Escape') {
      editingTitle = false;
    }
  }

  function applyEvent(event: SseEvent): void {
    if (event.type === 'server.connected') {
      sseDisconnected = false;
      reconnectDelay = 1000;
      error = '';
      return;
    }

    if (event.type === 'session.created' || event.type === 'session.updated') {
      upsertSession(event.properties.info);
      return;
    }

    if (event.type === 'session.deleted') {
      sessions = sessions.filter((session) => session.id !== event.properties.info.id);
      if (selectedSessionId === event.properties.info.id) {
        const nextSession = sessions[0]?.id ?? '';
        selectedSessionId = nextSession;
        void (nextSession ? selectSession(nextSession) : handleCreateSession());
      }
      return;
    }

    if (event.type === 'session.status') {
      statuses = { ...statuses, [event.properties.sessionID]: event.properties.status };
      return;
    }

    if (event.type === 'message.updated') {
      if (event.properties.info.sessionID !== selectedSessionId) return;
      upsertMessage(event.properties.info);
      autoScroll();
      return;
    }

    if (event.type === 'message.part.updated') {
      if (event.properties.part.sessionID !== selectedSessionId) return;
      upsertPart(event.properties.part);
      autoScroll();
    }
  }

  function upsertSession(session: SessionInfo): void {
    const next = sessions.filter((item: SessionInfo) => item.id !== session.id);
    next.push(session);
    sessions = sortSessions(next);
  }

  function upsertMessage(info: MessageInfo): void {
    const existing = messages.find((message) => message.info.id === info.id);
    if (existing) {
      existing.info = info;
      messages = [...messages];
      return;
    }

    messages = [...messages, { info, parts: [] }].sort(
      (left, right) => left.info.time.created - right.info.time.created
    );
  }

  function upsertPart(part: MessagePart): void {
    const target = messages.find((message) => message.info.id === part.messageID);
    if (!target) {
      messages = [...messages, { info: messageInfoFromPart(part), parts: [part] }].sort(
        (left, right) => left.info.time.created - right.info.time.created
      );
      return;
    }

    const existingIndex = target.parts.findIndex((item: MessagePart) => item.id === part.id);
    if (existingIndex >= 0) {
      const existing = target.parts[existingIndex];
      // Mutate text parts in-place for Svelte 5 fine-grained reactivity
      if (existing.type === 'text' && part.type === 'text') {
        existing.text = part.text || existing.text;
        existing.time = part.time;
        if (part.phase) existing.phase = part.phase;
      } else if (existing.type === 'tool' && part.type === 'tool') {
        Object.assign(existing.state, part.state);
        Object.assign(existing.state.input, part.state.input);
      } else {
        target.parts[existingIndex] = part;
      }
    } else {
      target.parts.push(part);
    }
  }

  function mergeParts(current: MessagePart, incoming: MessagePart): MessagePart {
    if (current.type === 'text' && incoming.type === 'text') {
      return { ...current, ...incoming, text: incoming.text || current.text };
    }

    if (current.type === 'tool' && incoming.type === 'tool') {
      return {
        ...current,
        ...incoming,
        state: {
          ...current.state,
          ...incoming.state,
          input: { ...current.state.input, ...incoming.state.input }
        }
      };
    }

    return incoming;
  }

  function messageInfoFromPart(part: MessagePart): MessageInfo {
    const created = part.type === 'text' ? part.time.start : part.state.time.start;

    return {
      id: part.messageID,
      sessionID: part.sessionID,
      role: 'assistant',
      time: { created }
    };
  }

  function sortSessions(items: SessionInfo[]): SessionInfo[] {
    return [...items].sort((left, right) => right.time.updated - left.time.updated);
  }

  function toErrorMessage(errorValue: unknown, fallback: string): string {
    if (errorValue instanceof Error && errorValue.message) {
      return `${fallback}: ${errorValue.message}`;
    }

    return fallback;
  }

  function updateDraft(value: string): void {
    draft = value;
  }

  function scrollToBottom(): void {
    if (!chatLogEl) return;
    chatLogEl.scrollTop = chatLogEl.scrollHeight;
    showScrollButton = false;
    // Double-tap: content may reflow after initial layout (markdown, images).
    requestAnimationFrame(() => {
      if (chatLogEl) chatLogEl.scrollTop = chatLogEl.scrollHeight;
    });
  }

  function autoScroll(): void {
    if (!chatLogEl) return;
    const { scrollTop, scrollHeight, clientHeight } = chatLogEl;
    const nearBottom = scrollHeight - scrollTop - clientHeight < 150;
    if (nearBottom) {
      void tick().then(scrollToBottom);
    }
  }

  function handleChatScroll(): void {
    if (!chatLogEl) return;
    const { scrollTop, scrollHeight, clientHeight } = chatLogEl;
    showScrollButton = scrollHeight - scrollTop - clientHeight > 150;
  }
</script>

<svelte:head>
  <title>Nanobot</title>
</svelte:head>

<svelte:window onkeydown={handleGlobalKeydown} />

<div class="app-shell" class:sidebar-collapsed={!sidebarOpen}>
  <aside class="sidebar" class:open={sidebarOpen}>
    <SessionSidebar
      {sessions}
      activeSessionId={selectedSessionId}
      {statuses}
      {creating}
      onCreate={handleCreateSession}
      onSelect={(id) => selectSession(id)}
      onDelete={handleDeleteSession}
    />
  </aside>
  <!-- svelte-ignore a11y_no_static_element_interactions -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div class="backdrop" class:visible={sidebarOpen} onclick={() => sidebarOpen = false}></div>

  <button class="sidebar-toggle" onclick={() => sidebarOpen = !sidebarOpen} aria-label="Toggle sidebar">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
      {#if sidebarOpen}
        <path d="M15 18l-6-6 6-6" />
      {:else}
        <path d="M9 18l6-6-6-6" />
      {/if}
    </svg>
  </button>

  <main class="chat-panel">
    <div class="chat-header">
      <div class="title-block">
        <div>
          {#if editingTitle}
            <!-- svelte-ignore a11y_autofocus -->
            <input
              class="title-input"
              type="text"
              bind:value={titleDraft}
              onblur={handleTitleSubmit}
              onkeydown={handleTitleKeydown}
              autofocus
            />
          {:else}
            <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
            <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
            <h2
              class="title-editable"
              role="button"
              tabindex="0"
              onclick={() => { editingTitle = true; titleDraft = currentSession?.title || ''; }}
              onkeydown={(e) => { if (e.key === 'Enter') { editingTitle = true; titleDraft = currentSession?.title || ''; }}}
            >{currentSession?.title || 'New Session'}</h2>
          {/if}
          {#if currentSession && !editingTitle}
            <span class="session-id">{currentSession.id}</span>
          {/if}
        </div>
        <div class="status-group">
          {#if sseDisconnected}
            <span class="sse-pill" title="Connection lost — reconnecting...">
              <span class="sse-dot"></span> Offline
            </span>
          {/if}
          {#if currentSession}
            {#if isBusy}
              <span class="status-badge busy">
                <span class="pulse"></span>
                Working
              </span>
            {:else}
              <span class="status-badge idle">Idle</span>
            {/if}
          {/if}
          <button
            class="log-toggle"
            class:active={logsOpen}
            onclick={() => logsOpen = !logsOpen}
            title="Toggle logs"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M4 19h16M4 15h16M4 11h10M4 7h6" />
            </svg>
          </button>
        </div>
      </div>
      {#if contextInfo?.usagePercent != null}
        <div class="token-row">
          <div
            class="token-bar"
            title="{Math.round(contextInfo.usagePercent)}% context used"
          >
            <div
              class="token-bar-fill"
              class:over-budget={!contextInfo.withinBudget}
              style="width: {Math.min(contextInfo.usagePercent, 100)}%"
            ></div>
          </div>
          {#if contextInfo.compactionPasses && contextInfo.compactionPasses > 0}
            <span class="compaction-tag" title="Context was compacted to fit budget">
              compacted
            </span>
          {/if}
        </div>
      {/if}
    </div>

    {#if error}
      <div class="banner error">
        <span>{error}</span>
        <button class="dismiss" onclick={() => error = ''}>dismiss</button>
      </div>
    {/if}

    {#if sessionsLoading}
      <div class="chat-window">
        <div class="empty-chat">
          <div class="loading-dots"><span></span><span></span><span></span></div>
          <p>Loading sessions...</p>
        </div>
      </div>
    {:else}
      <div class="chat-window-wrapper">
        <div class="chat-log" bind:this={chatLogEl} onscroll={handleChatScroll}>
          {#if messagesLoading}
            <div class="empty-chat">
              <div class="loading-dots"><span></span><span></span><span></span></div>
              <p>Loading messages...</p>
            </div>
          {:else if messages.length === 0}
            <div class="empty-chat">
              <p>No messages yet</p>
              <span>Send a message to begin.</span>
            </div>
          {:else}
            {#each messages as message (message.info.id)}
              <ChatMessage {message} />
            {/each}
            {#if isBusy}
              <div class="typing-indicator">
                <div class="loading-dots"><span></span><span></span><span></span></div>
              </div>
            {/if}
          {/if}
        </div>

        {#if showScrollButton}
          <button class="scroll-bottom" onclick={scrollToBottom} aria-label="Scroll to bottom">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
              <path d="M7 13l5 5 5-5M7 6l5 5 5-5" />
            </svg>
          </button>
        {/if}
      </div>

      <LogViewer open={logsOpen} onToggle={() => logsOpen = false} />

      <div class="composer-panel">
        <Composer
          value={draft}
          disabled={!selectedSessionId || sending}
          models={availableModels}
          {selectedModel}
          onInput={updateDraft}
          onSend={handleSend}
          onModelChange={handleModelChange}
          onAbort={isBusy ? handleAbort : undefined}
        />
      </div>
    {/if}
  </main>
</div>

<style>
  .app-shell {
    display: grid;
    grid-template-columns: 280px 1fr;
    height: 100vh;
    height: 100dvh;
    transition: grid-template-columns 200ms ease;
  }

  .app-shell.sidebar-collapsed {
    grid-template-columns: 0px 1fr;
  }

  .sidebar {
    background: var(--panel-strong);
    border-right: 1px solid var(--border);
    overflow: hidden;
    transition: opacity 200ms ease;
    padding: 1rem;
  }

  .sidebar-collapsed .sidebar {
    opacity: 0;
    pointer-events: none;
    padding: 0;
  }

  .sidebar-toggle {
    position: fixed;
    top: 0.75rem;
    left: 0;
    z-index: 20;
    background: var(--panel-strong);
    border: 1px solid var(--border);
    border-left: none;
    border-radius: 0 0.5rem 0.5rem 0;
    color: var(--muted);
    cursor: pointer;
    padding: 0.4rem 0.35rem 0.4rem 0.25rem;
    transition: color 150ms, left 200ms ease;
  }

  .app-shell:not(.sidebar-collapsed) .sidebar-toggle {
    left: 280px;
  }

  .sidebar-toggle:hover {
    color: var(--text);
  }

  .chat-panel {
    display: grid;
    grid-template-rows: auto auto 1fr auto;
    min-height: 0;
    min-width: 0;
    height: 100vh;
    height: 100dvh;
  }

  .chat-header {
    padding: 1rem 1.25rem;
    border-bottom: 1px solid var(--border);
  }

  .title-block {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  h2 {
    margin: 0;
    font-size: 1.1rem;
    font-weight: 600;
    line-height: 1.3;
  }

  .session-id {
    font-size: 0.72rem;
    color: var(--muted);
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
    opacity: 0.7;
  }

  .status-group {
    flex-shrink: 0;
  }

  .status-badge {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.75rem;
    font-weight: 500;
    padding: 0.3rem 0.65rem;
    border-radius: 999px;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .status-badge.idle {
    color: var(--muted);
    background: var(--surface);
    border: 1px solid var(--border);
  }

  .status-badge.busy {
    color: #0a1117;
    background: var(--accent);
    border: 1px solid var(--accent);
  }

  .pulse {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: currentColor;
    animation: pulse-anim 1.4s ease-in-out infinite;
  }

  @keyframes pulse-anim {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  .banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.6rem 1rem;
    font-size: 0.85rem;
  }

  .banner.error {
    color: #fca5a5;
    background: rgba(127, 29, 29, 0.25);
    border-bottom: 1px solid rgba(248, 113, 113, 0.2);
  }

  .dismiss {
    background: none;
    border: 1px solid rgba(248, 113, 113, 0.3);
    border-radius: 0.4rem;
    color: inherit;
    font-size: 0.72rem;
    padding: 0.2rem 0.5rem;
    cursor: pointer;
    opacity: 0.7;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .dismiss:hover {
    opacity: 1;
  }

  .chat-window-wrapper {
    position: relative;
    min-height: 0;
    min-width: 0;
    overflow: hidden;
  }

  .chat-log {
    height: 100%;
    min-width: 0;
    overflow-y: auto;
    padding: 1rem 1.25rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .empty-chat {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    height: 100%;
    color: var(--muted);
    text-align: center;
    gap: 0.5rem;
  }

  .empty-chat p {
    margin: 0;
    font-size: 1rem;
    color: var(--text);
    opacity: 0.6;
  }

  .empty-chat span {
    font-size: 0.85rem;
    opacity: 0.5;
  }

  .typing-indicator {
    padding: 0.5rem 0;
  }

  .loading-dots {
    display: flex;
    gap: 4px;
    align-items: center;
  }

  .loading-dots span {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--accent);
    animation: dot-bounce 1.2s ease-in-out infinite;
  }

  .loading-dots span:nth-child(2) {
    animation-delay: 0.15s;
  }

  .loading-dots span:nth-child(3) {
    animation-delay: 0.3s;
  }

  @keyframes dot-bounce {
    0%, 80%, 100% { opacity: 0.25; transform: scale(0.8); }
    40% { opacity: 1; transform: scale(1); }
  }

  .scroll-bottom {
    position: absolute;
    bottom: 0.75rem;
    right: 1.25rem;
    z-index: 5;
    background: var(--panel-strong);
    border: 1px solid var(--border);
    border-radius: 50%;
    width: 2rem;
    height: 2rem;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--muted);
    cursor: pointer;
    transition: color 150ms, border-color 150ms;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.3);
  }

  .scroll-bottom:hover {
    color: var(--text);
    border-color: var(--border-hover);
  }

  .composer-panel {
    border-top: 1px solid var(--border);
    padding: 0.75rem 1.25rem;
  }

  .backdrop {
    display: none;
  }

  .title-editable {
    cursor: pointer;
    transition: color 150ms;
  }

  .title-editable:hover {
    color: var(--accent);
  }

  .title-input {
    font-size: 1.1rem;
    font-weight: 600;
    background: var(--surface);
    border: 1px solid rgba(110, 231, 168, 0.3);
    border-radius: 0.35rem;
    color: var(--text);
    padding: 0.15rem 0.4rem;
    font: inherit;
    font-size: 1.1rem;
    font-weight: 600;
    outline: none;
    width: 100%;
  }

  .log-toggle {
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 0.3rem;
    color: var(--muted);
    padding: 0.2rem 0.35rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    transition: color 150ms, border-color 150ms;
  }

  .log-toggle:hover,
  .log-toggle.active {
    color: var(--accent);
    border-color: var(--accent);
  }

  .sse-pill {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    font-size: 0.7rem;
    font-weight: 500;
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.1);
    border: 1px solid rgba(251, 191, 36, 0.2);
    padding: 0.2rem 0.5rem;
    border-radius: 999px;
  }

  .sse-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: #fbbf24;
    animation: pulse-anim 1.4s ease-in-out infinite;
  }

  .token-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .token-bar {
    flex: 1;
    height: 3px;
    background: var(--surface);
    border-radius: 1.5px;
    overflow: hidden;
  }

  .token-bar-fill {
    height: 100%;
    background: var(--accent);
    border-radius: 1.5px;
    transition: width 300ms ease;
  }

  .token-bar-fill.over-budget {
    background: var(--danger);
  }

  .compaction-tag {
    flex-shrink: 0;
    font-size: 0.6rem;
    font-weight: 500;
    color: #fbbf24;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    opacity: 0.8;
  }

  @media (max-width: 768px) {
    .app-shell,
    .app-shell.sidebar-collapsed {
      grid-template-columns: 1fr;
    }

    .sidebar {
      position: fixed;
      top: 0;
      left: 0;
      bottom: 0;
      width: 280px;
      z-index: 30;
      transform: translateX(0);
      transition: transform 200ms ease;
    }

    .sidebar-collapsed .sidebar {
      transform: translateX(-100%);
      opacity: 1;
    }

    .app-shell:not(.sidebar-collapsed) .sidebar-toggle {
      left: 280px;
    }

    .sidebar-toggle {
      z-index: 31;
    }

    .backdrop.visible {
      display: block;
      position: fixed;
      inset: 0;
      z-index: 29;
      background: rgba(0, 0, 0, 0.5);
    }

    .chat-header {
      padding: 0.65rem 0.75rem;
    }

    .chat-log {
      padding: 0.65rem 0.5rem;
    }

    .composer-panel {
      padding: 0.5rem 0.5rem;
      /* extra bottom padding for mobile browser toolbar */
      padding-bottom: calc(0.5rem + env(safe-area-inset-bottom, 0px));
    }

    h2 {
      font-size: 0.95rem;
    }
  }
</style>
