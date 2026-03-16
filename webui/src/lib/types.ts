export interface SessionInfo {
  id: string;
  projectID: string;
  directory: string;
  title: string;
  version: string;
  model?: string;
  time: {
    created: number;
    updated: number;
  };
}

export interface TokenInfo {
  input: number;
  output: number;
  reasoning: number;
  cache: { read: number; write: number };
}

export interface MessageInfo {
  id: string;
  sessionID: string;
  role: 'user' | 'assistant';
  time: {
    created: number;
    completed?: number;
  };
  parentID?: string;
  modelID?: string;
  providerID?: string;
  mode?: string;
  agent?: string;
  path?: {
    cwd: string;
    root: string;
  };
  cost?: number;
  tokens?: TokenInfo;
}

export interface ToolState {
  status: 'running' | 'completed' | 'error';
  input: Record<string, string>;
  output?: string;
  error?: string;
  title?: string;
  metadata?: Record<string, unknown>;
  time: {
    start: number;
    end?: number;
  };
}

export interface TextPart {
  id: string;
  sessionID: string;
  messageID: string;
  type: 'text';
  text: string;
  time: { start: number; end?: number };
  phase?: 'thinking' | 'assistant';
  delta?: string;
}

export interface ToolPart {
  id: string;
  sessionID: string;
  messageID: string;
  type: 'tool';
  callID: string;
  tool: string;
  state: ToolState;
}

export type MessagePart = TextPart | ToolPart;

export interface MessageWithParts {
  info: MessageInfo;
  parts: MessagePart[];
}

export interface SessionStatus {
  sessionID: string;
  status: {
    type: 'idle' | 'busy';
    context?: {
      budget?: number;
      usagePercent?: number;
      withinBudget?: boolean;
      contextTokens?: number;
      final?: { total: number };
      model?: string;
      tokens?: { used: number; remaining: number };
      mode?: string;
      compactionPasses?: number;
      trimmedHistoryMessages?: number;
    };
  };
}

export interface ModelInfo {
  id: string;
  providerID: string;
  name: string;
}

export interface ProviderInfo {
  id: string;
  name: string;
  models: Record<string, ModelInfo>;
}

export interface ApiError {
  error: string;
}

export type SseEvent =
  | { type: 'server.connected'; properties: Record<string, never> }
  | { type: 'server.heartbeat'; properties: Record<string, never> }
  | { type: 'session.created'; properties: { info: SessionInfo } }
  | { type: 'session.updated'; properties: { info: SessionInfo } }
  | { type: 'session.deleted'; properties: { info: SessionInfo } }
  | {
      type: 'session.status';
      properties: {
        sessionID: string;
        status: SessionStatus['status'];
      };
    }
  | { type: 'message.updated'; properties: { info: MessageInfo } }
  | { type: 'message.part.updated'; properties: { part: MessagePart; delta?: string } };
