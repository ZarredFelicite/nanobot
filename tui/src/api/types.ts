// Wire format types matching nanobot's OpenCode API

export interface SessionInfo {
  id: string;
  projectID: string;
  directory: string;
  title: string;
  version: string;
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
  role: "user" | "assistant";
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
  status: "running" | "completed" | "error";
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

export type MessagePart = TextPart | ToolPart;

export interface TextPart {
  id: string;
  sessionID: string;
  messageID: string;
  type: "text";
  text: string;
  time: { created: number };
  delta?: string;
}

export interface ToolPart {
  id: string;
  sessionID: string;
  messageID: string;
  type: "tool";
  callID: string;
  tool: string;
  state: ToolState;
}

export interface SessionStatus {
  sessionID: string;
  status: {
    type: "idle" | "busy";
    context?: {
      tokens?: { used: number; remaining: number };
      mode?: string;
    };
  };
}

export interface PermissionRequest {
  id: string;
  sessionID: string;
  permission: string;
  patterns: string[];
  metadata?: Record<string, unknown>;
  always?: string[];
  tool?: { callID: string };
}

export type PermissionReply = "once" | "always" | "reject";

// SSE event discriminated union
export type SSEEvent =
  | { type: "server.connected"; properties: Record<string, never> }
  | { type: "server.heartbeat"; properties: Record<string, never> }
  | { type: "session.created"; properties: { info: SessionInfo } }
  | { type: "session.updated"; properties: { info: SessionInfo } }
  | { type: "session.deleted"; properties: { info: SessionInfo } }
  | {
      type: "session.status";
      properties: {
        sessionID: string;
        status: SessionStatus["status"];
      };
    }
  | { type: "message.updated"; properties: { info: MessageInfo } }
  | {
      type: "message.part.updated";
      properties: { part: MessagePart; delta?: string };
    }
  | {
      type: "permission.asked";
      properties: PermissionRequest;
    }
  | {
      type: "permission.replied";
      properties: {
        sessionID: string;
        requestID: string;
        reply: PermissionReply;
      };
    };

// Message with parts (history response format)
export interface MessageWithParts {
  info: MessageInfo;
  parts: MessagePart[];
}

// Bootstrap data
export interface ProviderModel {
  id: string;
  providerID: string;
  name: string;
  capabilities: Record<string, unknown>;
  cost: Record<string, number>;
  limit: { context: number; output: number };
  status: string;
}

export interface Provider {
  id: string;
  name: string;
  source: string;
  models: Record<string, ProviderModel>;
}

export interface ProviderCatalog {
  providers: Provider[];
  defaultModel: string;
}
