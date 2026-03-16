import type { MessageWithParts, ProviderInfo, SessionInfo, SessionStatus } from '$lib/types';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...init?.headers
    }
  });

  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText}`);
  }

  return (await response.json()) as T;
}

export function listSessions(): Promise<SessionInfo[]> {
  return request('/session');
}

export function createSession(title?: string): Promise<SessionInfo> {
  return request('/session', {
    method: 'POST',
    body: JSON.stringify(title ? { title } : {})
  });
}

export function getMessages(sessionId: string): Promise<MessageWithParts[]> {
  return request(`/session/${encodeURIComponent(sessionId)}/message`);
}

export function sendMessage(sessionId: string, text: string, model?: string): Promise<unknown> {
  const body: Record<string, unknown> = {
    parts: [{ type: 'text', text }]
  };
  if (model) {
    body.model = model;
  }
  return request(`/session/${encodeURIComponent(sessionId)}/message`, {
    method: 'POST',
    body: JSON.stringify(body)
  });
}

export function getStatuses(): Promise<SessionStatus[]> {
  return request('/session/status');
}

export function deleteSession(sessionId: string): Promise<unknown> {
  return request(`/session/${encodeURIComponent(sessionId)}`, { method: 'DELETE' });
}

export function abortSession(sessionId: string): Promise<unknown> {
  return request(`/session/${encodeURIComponent(sessionId)}/abort`, { method: 'POST' });
}

export function patchSession(sessionId: string, body: Record<string, unknown>): Promise<unknown> {
  return request(`/session/${encodeURIComponent(sessionId)}`, {
    method: 'PATCH',
    body: JSON.stringify(body)
  });
}

export function getProviders(): Promise<ProviderInfo[]> {
  return request('/provider');
}
