import {
  TUI,
  ProcessTerminal,
  Container,
  Text,
  Loader,
  Editor,
  matchesKey,
  Key,
  type OverlayHandle,
  CombinedAutocompleteProvider,
} from "@mariozechner/pi-tui";

import { NanobotClient } from "../api/client.js";
import { SSEConnection } from "../api/sse.js";
import type {
  SSEEvent,
  SessionInfo,
  MessageInfo,
  MessagePart,
  PermissionRequest,
} from "../api/types.js";

import { editorTheme, colors } from "./theme.js";
import { ChatLog } from "./components/chat-log.js";
import { FooterComponent } from "./components/footer.js";
import { PermissionDialogComponent } from "./components/permission-dialog.js";
import { SessionSelectorComponent } from "./components/session-selector.js";

export class App {
  private client: NanobotClient;
  private tui: TUI;
  private terminal: ProcessTerminal;
  private sse: SSEConnection;

  private chatLog: ChatLog;
  private statusContainer: Container;
  private editorContainer: Container;

  private footer: FooterComponent;
  private editor: Editor;
  private statusLoader: Loader | null = null;
  private overlay: OverlayHandle | null = null;

  private activeSessionId = "";
  private isBusy = false;
  private defaultModel = "";

  constructor(client: NanobotClient) {
    this.client = client;
    this.terminal = new ProcessTerminal();
    this.tui = new TUI(this.terminal);

    this.chatLog = new ChatLog(this.tui);
    this.statusContainer = new Container();
    this.editorContainer = new Container();
    this.footer = new FooterComponent();

    this.tui.addChild(this.chatLog);
    this.tui.addChild(this.statusContainer);
    this.tui.addChild(this.editorContainer);
    this.tui.addChild(this.footer.container);

    this.editor = new Editor(this.tui, editorTheme, { paddingX: 1 });
    this.editor.onSubmit = (text) => this.handleSubmit(text);
    this.editorContainer.addChild(this.editor);

    const slashCommands = [
      { name: "/new", description: "Create a new session" },
      { name: "/sessions", description: "Switch session" },
      { name: "/model", description: "Switch model" },
      { name: "/abort", description: "Abort current request" },
      { name: "/compact", description: "Summarize session" },
    ];
    this.editor.setAutocompleteProvider(
      new CombinedAutocompleteProvider(slashCommands)
    );

    this.sse = new SSEConnection(
      client.getEventUrl(),
      (event) => this.handleSSEEvent(event)
    );

    this.tui.addInputListener((data) => {
      if (this.overlay) return undefined;

      if (matchesKey(data, Key.ctrl("c"))) {
        if (this.isBusy) {
          this.client.abortSession(this.activeSessionId).catch(() => {});
          return { consume: true };
        }
        if (this.editor.getText().trim() === "") {
          this.shutdown();
          return { consume: true };
        }
      }

      if (matchesKey(data, Key.escape) && this.isBusy) {
        this.client.abortSession(this.activeSessionId).catch(() => {});
        return { consume: true };
      }

      return undefined;
    });
  }

  async start(): Promise<void> {
    this.tui.start();

    const connecting = new Text(colors.dim("Connecting to nanobot..."), 1, 0);
    this.chatLog.addChild(connecting);
    this.tui.requestRender();

    try {
      const [providers, sessions] = await Promise.all([
        this.client.getProviders(),
        this.client.listSessions(),
      ]);

      this.defaultModel = providers.defaultModel;
      this.footer.setModel(this.defaultModel);

      const session =
        sessions.length > 0
          ? sessions.sort((a, b) => b.time.updated - a.time.updated)[0]
          : await this.client.createSession();

      this.activeSessionId = session.id;
      this.footer.setSession(session.title || session.id);

      await this.loadHistory(session.id);
      this.chatLog.removeChild(connecting);

      this.sse.connect();
      this.tui.setFocus(this.editor);
      this.tui.requestRender();
    } catch (err) {
      this.chatLog.removeChild(connecting);
      this.chatLog.addSystem(
        `Failed to connect: ${err instanceof Error ? err.message : String(err)}`
      );
      this.tui.requestRender();
    }
  }

  private async loadHistory(sessionId: string): Promise<void> {
    try {
      const messages = await this.client.getMessages(sessionId);
      this.chatLog.clearAll();
      for (const msg of messages) {
        this.chatLog.addHistoryMessage(msg);
      }
    } catch {
      // No history or error - that's fine
    }
  }

  private handleSSEEvent(event: SSEEvent): void {
    switch (event.type) {
      case "session.status":
        if (event.properties.sessionID !== this.activeSessionId) return;
        this.handleSessionStatus(event.properties.status);
        break;

      case "message.updated":
        if (event.properties.info.sessionID !== this.activeSessionId) return;
        this.handleMessageUpdated(event.properties.info);
        break;

      case "message.part.updated":
        if (event.properties.part.sessionID !== this.activeSessionId) return;
        this.handlePartUpdated(event.properties.part);
        break;

      case "permission.asked":
        if (event.properties.sessionID !== this.activeSessionId) return;
        this.showPermissionDialog(event.properties);
        break;

      case "session.created":
      case "session.updated":
        if (event.properties.info.id === this.activeSessionId) {
          this.footer.setSession(
            event.properties.info.title || event.properties.info.id
          );
        }
        break;
    }

    this.tui.requestRender();
  }

  private handleSessionStatus(status: { type: string; context?: Record<string, unknown> }): void {
    const busy = status.type === "busy";
    this.isBusy = busy;
    this.footer.setBusy(busy);

    if (busy) {
      if (!this.statusLoader) {
        this.statusLoader = new Loader(
          this.tui,
          colors.accent,
          colors.dim,
          "Thinking..."
        );
        this.statusLoader.start();
        this.statusContainer.addChild(this.statusLoader);
      }
    } else if (this.statusLoader) {
      this.statusLoader.stop();
      this.statusContainer.removeChild(this.statusLoader);
      this.statusLoader = null;
    }

    if (status.context) {
      const tokens = status.context.tokens as { used?: number } | undefined;
      if (tokens?.used) {
        this.footer.setTokens(tokens.used);
      }
    }
  }

  private handleMessageUpdated(info: MessageInfo): void {
    this.chatLog.upsertMessageInfo(info);
  }

  private handlePartUpdated(part: MessagePart): void {
    if (part.type === "text") {
      this.chatLog.upsertTextPart(part);
    } else if (part.type === "tool") {
      this.chatLog.upsertToolPart(part);
    }
  }

  private showPermissionDialog(request: PermissionRequest): void {
    const dialog = new PermissionDialogComponent(request);
    dialog.onReply = async (requestId, reply) => {
      try {
        await this.client.replyPermission(requestId, reply);
      } catch {
        // Ignore transient UI errors here.
      }
      this.overlay?.hide();
      this.overlay = null;
      this.tui.setFocus(this.editor);
    };

    this.overlay = this.tui.showOverlay(dialog, {
      anchor: "center",
      width: "60%",
      maxHeight: "40%",
    });
  }

  private async handleSubmit(text: string): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed) return;

    const normalized = trimmed.replace(/^\/{2,}/, "/");
    this.editor.addToHistory(normalized);

    if (normalized.startsWith("/")) {
      await this.handleSlashCommand(normalized);
      return;
    }

    try {
      await this.client.sendMessage(this.activeSessionId, normalized);
    } catch (err) {
      this.chatLog.addSystem(
        `Send failed: ${err instanceof Error ? err.message : String(err)}`
      );
      this.tui.requestRender();
    }
  }

  private async handleSlashCommand(input: string): Promise<void> {
    const parts = input.split(/\s+/);
    const cmd = parts[0];

    switch (cmd) {
      case "/new": {
        const session = await this.client.createSession(parts.slice(1).join(" ") || undefined);
        await this.switchSession(session);
        break;
      }

      case "/sessions": {
        await this.showSessionSelector();
        break;
      }

      case "/model": {
        const modelName = parts.slice(1).join(" ");
        if (modelName) {
          this.defaultModel = modelName;
          this.footer.setModel(modelName);
          this.chatLog.addSystem(`Model set to ${modelName}`);
        } else {
          await this.showModelSelector();
        }
        break;
      }

      case "/abort": {
        if (this.isBusy) {
          await this.client.abortSession(this.activeSessionId);
        }
        break;
      }

      case "/compact": {
        await this.client.summarizeSession(this.activeSessionId);
        this.chatLog.addSystem("Session summarized");
        break;
      }

      default:
        this.chatLog.addSystem(`Unknown command: ${cmd}`);
    }
    this.tui.requestRender();
  }

  private async switchSession(session: SessionInfo): Promise<void> {
    this.activeSessionId = session.id;
    this.footer.setSession(session.title || session.id);

    this.chatLog.clearAll();

    if (this.statusLoader) {
      this.statusLoader.stop();
      this.statusContainer.removeChild(this.statusLoader);
      this.statusLoader = null;
    }

    await this.loadHistory(session.id);
    this.tui.requestRender();
  }

  private async showSessionSelector(): Promise<void> {
    try {
      const sessions = await this.client.listSessions();
      if (sessions.length === 0) {
        this.chatLog.addSystem("No sessions found");
        this.tui.requestRender();
        return;
      }

      const selector = new SessionSelectorComponent(sessions);
      selector.onSelect = async (session) => {
        this.overlay?.hide();
        this.overlay = null;
        await this.switchSession(session);
        this.tui.setFocus(this.editor);
      };
      selector.onCancel = () => {
        this.overlay?.hide();
        this.overlay = null;
        this.tui.setFocus(this.editor);
      };

      this.overlay = this.tui.showOverlay(selector, {
        anchor: "center",
        width: "70%",
        maxHeight: "60%",
      });
    } catch (err) {
      this.chatLog.addSystem(`Failed to list sessions: ${err}`);
      this.tui.requestRender();
    }
  }

  private async showModelSelector(): Promise<void> {
    try {
      const catalog = await this.client.getProviders();
      const items = catalog.providers.flatMap((p) =>
        Object.values(p.models).map((m) => ({
          value: `${p.id}/${m.id}`,
          label: m.name,
          description: p.name,
        }))
      );

      const { SelectList } = await import("@mariozechner/pi-tui");
      const { selectListTheme } = await import("./theme.js");

      const list = new SelectList(items, 10, selectListTheme);
      list.onSelect = (item) => {
        this.defaultModel = item.value;
        this.footer.setModel(item.value);
        this.chatLog.addSystem(`Model: ${item.value}`);
        this.overlay?.hide();
        this.overlay = null;
        this.tui.setFocus(this.editor);
        this.tui.requestRender();
      };
      list.onCancel = () => {
        this.overlay?.hide();
        this.overlay = null;
        this.tui.setFocus(this.editor);
      };

      this.overlay = this.tui.showOverlay(list, {
        anchor: "center",
        width: "60%",
        maxHeight: "50%",
      });
    } catch (err) {
      this.chatLog.addSystem(`Failed to list models: ${err}`);
      this.tui.requestRender();
    }
  }

  private shutdown(): void {
    this.chatLog.clearAll();
    if (this.statusLoader) this.statusLoader.stop();

    this.sse.disconnect();
    this.tui.stop();
    process.exit(0);
  }
}
