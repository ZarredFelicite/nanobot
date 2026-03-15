import type {
  AppConfig,
  SessionInfo,
  MessageInfo,
  MessagePart,
  MessageWithParts,
  ProviderCatalog,
  ProviderCatalogResponse,
  PermissionReply,
  SessionStatus,
} from "./types.js";

export class NanobotClient {
  private baseUrl: string;

  constructor(host: string = "127.0.0.1", port: number = 18790) {
    this.baseUrl = `http://${host}:${port}`;
  }

  private async request<T>(
    path: string,
    options?: RequestInit
  ): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...options?.headers,
      },
    });
    if (!res.ok) {
      throw new Error(`API ${res.status}: ${await res.text()}`);
    }
    return res.json() as Promise<T>;
  }

  // Bootstrap
  async getProviders(): Promise<ProviderCatalog> {
    const response = await this.request<ProviderCatalogResponse>("/config/providers");
    return {
      providers: response.providers,
      defaultModel: response.default?.default || "",
    };
  }

  async getConfig(): Promise<AppConfig> {
    return this.request("/config");
  }

  async getAgent(): Promise<Record<string, unknown>> {
    return this.request("/agent");
  }

  async healthCheck(): Promise<Record<string, unknown>> {
    return this.request("/global/health");
  }

  // Sessions
  async createSession(title?: string): Promise<SessionInfo> {
    return this.request("/session", {
      method: "POST",
      body: JSON.stringify(title ? { title } : {}),
    });
  }

  async listSessions(): Promise<SessionInfo[]> {
    return this.request("/session");
  }

  async getSession(id: string): Promise<SessionInfo> {
    return this.request(`/session/${id}`);
  }

  async deleteSession(id: string): Promise<void> {
    await this.request(`/session/${id}`, { method: "DELETE" });
  }

  async patchSession(
    id: string,
    data: Partial<SessionInfo> & Record<string, unknown>
  ): Promise<SessionInfo> {
    return this.request(`/session/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  }

  async setSessionModel(id: string, model: string): Promise<SessionInfo> {
    return this.patchSession(id, { model });
  }

  async getSessionStatuses(): Promise<SessionStatus[]> {
    return this.request("/session/status");
  }

  // Messages
  async getMessages(sessionId: string): Promise<MessageWithParts[]> {
    return this.request(`/session/${sessionId}/message`);
  }

  async sendMessage(
    sessionId: string,
    text: string,
    model?: string
  ): Promise<MessageInfo> {
    const body: Record<string, unknown> = {
      parts: [{ type: "text", text }],
    };
    if (model) body.model = model;
    return this.request(`/session/${sessionId}/message`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  async abortSession(sessionId: string): Promise<void> {
    await this.request(`/session/${sessionId}/abort`, { method: "POST" });
  }

  // Summarize
  async summarizeSession(sessionId: string): Promise<void> {
    await this.request(`/session/${sessionId}/summarize`, { method: "POST" });
  }

  // Permissions
  async replyPermission(
    requestId: string,
    reply: PermissionReply
  ): Promise<void> {
    await this.request(`/permission/${requestId}/reply`, {
      method: "POST",
      body: JSON.stringify({ reply }),
    });
  }

  // Commands
  async listCommands(): Promise<Record<string, unknown>[]> {
    return this.request("/command");
  }

  async executeCommand(
    sessionId: string,
    command: string
  ): Promise<void> {
    await this.request(`/session/${sessionId}/command`, {
      method: "POST",
      body: JSON.stringify({ command }),
    });
  }

  getEventUrl(): string {
    return `${this.baseUrl}/event`;
  }

  getGlobalEventUrl(): string {
    return `${this.baseUrl}/global/event`;
  }
}
