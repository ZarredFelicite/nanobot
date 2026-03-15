import type { SSEEvent } from "./types.js";

export type SSEEventHandler = (event: SSEEvent) => void;

export class SSEConnection {
  private url: string;
  private handler: SSEEventHandler;
  private abortController: AbortController | null = null;
  private reconnectDelay = 1000;
  private maxReconnectDelay = 30000;
  private running = false;

  constructor(url: string, handler: SSEEventHandler) {
    this.url = url;
    this.handler = handler;
  }

  async connect(): Promise<void> {
    this.running = true;
    this.reconnectDelay = 1000;
    await this.doConnect();
  }

  private async doConnect(): Promise<void> {
    while (this.running) {
      try {
        this.abortController = new AbortController();
        const res = await fetch(this.url, {
          headers: { Accept: "text/event-stream" },
          signal: this.abortController.signal,
        });

        if (!res.ok) {
          throw new Error(`SSE ${res.status}: ${res.statusText}`);
        }

        const reader = res.body?.getReader();
        if (!reader) throw new Error("No response body");

        const decoder = new TextDecoder();
        let buffer = "";

        this.reconnectDelay = 1000; // Reset on successful connection

        while (this.running) {
          const { done, value } = await reader.read();
          if (done) break;

          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";

          for (const line of lines) {
            if (line.startsWith("data: ")) {
              try {
                const event = JSON.parse(line.slice(6)) as SSEEvent;
                this.handler(event);
              } catch {
                // Skip malformed JSON
              }
            }
          }
        }
      } catch (err) {
        if (!this.running) return;
        // Wait before reconnecting
        await new Promise((r) => setTimeout(r, this.reconnectDelay));
        this.reconnectDelay = Math.min(
          this.reconnectDelay * 2,
          this.maxReconnectDelay
        );
      }
    }
  }

  disconnect(): void {
    this.running = false;
    this.abortController?.abort();
    this.abortController = null;
  }
}
