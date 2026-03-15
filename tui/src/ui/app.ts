import {
  TUI,
  ProcessTerminal,
  Container,
  Spacer,
  Text,
  Loader,
  Editor,
  matchesKey,
  Key,
  type Component,
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
  ProviderCatalog,
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
  private providerCatalog: ProviderCatalog | null = null;
  private slashCommands = [
    { name: "/new", description: "Create a new session" },
    { name: "/sessions", description: "Switch session" },
    { name: "/model", description: "Switch model" },
    { name: "/abort", description: "Abort current request" },
    { name: "/compact", description: "Summarize session" },
  ];

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
    this.tui.addChild(this.footer);

    this.editor = new Editor(this.tui, editorTheme, { paddingX: 1 });
    this.editor.onSubmit = (text) => this.handleSubmit(text);
    this.editorContainer.addChild(this.editor);

    this.editor.setAutocompleteProvider(
      new CombinedAutocompleteProvider(this.slashCommands)
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
      const [providers, sessions, commands] = await Promise.all([
        this.client.getProviders(),
        this.client.listSessions(),
        this.client.listCommands().catch(() => []),
      ]);

      this.providerCatalog = providers;
      this.defaultModel = providers.defaultModel;
      this.updateFooterModel(this.defaultModel);
      this.applyServerCommands(commands);

      const session =
        sessions.length > 0
          ? sessions.sort((a, b) => b.time.updated - a.time.updated)[0]
          : await this.client.createSession();

      this.activeSessionId = session.id;

      const historyCount = await this.loadHistory(session.id);
      if (historyCount === 0) {
        this.chatLog.setEmptyState(this.buildEmptyState());
      }
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

  private async loadHistory(sessionId: string): Promise<number> {
    try {
      const messages = await this.client.getMessages(sessionId);
      this.chatLog.clearAll();
      for (const msg of messages) {
        this.chatLog.addHistoryMessage(msg);
      }
      return messages.length;
    } catch {
      // No history or error - that's fine
      return 0;
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
        break;
    }

    this.tui.requestRender();
  }

  private handleSessionStatus(status: { type: string; context?: Record<string, unknown> }): void {
    const busy = status.type === "busy";
    this.isBusy = busy;

    if (busy) {
      if (!this.statusLoader) {
        this.statusLoader = new Loader(
          this.tui,
          colors.dim,
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
      const tokens = status.context.tokens as { used?: number; remaining?: number } | undefined;
      if (typeof tokens?.used === "number" || typeof tokens?.remaining === "number") {
        this.footer.setContextUsage(tokens?.used ?? 0, tokens?.remaining ?? 0);
      }

      const mode = status.context.mode;
      if (typeof mode === "string" && mode) {
        this.footer.setThinkingLevel(mode);
      }
    }
  }

  private handleMessageUpdated(info: MessageInfo): void {
    this.chatLog.clearEmptyState();
    this.syncFooterFromMessage(info);
    this.chatLog.upsertMessageInfo(info);
  }

  private handlePartUpdated(part: MessagePart): void {
    this.chatLog.clearEmptyState();
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
      await this.client.sendMessage(
        this.activeSessionId,
        normalized,
        this.defaultModel || undefined
      );
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
          try {
            await this.client.setSessionModel(this.activeSessionId, modelName);
            this.defaultModel = modelName;
            this.updateFooterModel(modelName);
            this.chatLog.addSystem(`Model set to ${modelName}`);
          } catch (err) {
            this.chatLog.addSystem(
              `Failed to set model: ${err instanceof Error ? err.message : String(err)}`
            );
          }
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
        try {
          await this.client.executeCommand(this.activeSessionId, cmd);
        } catch (err) {
          this.chatLog.addSystem(
            `Unknown command: ${cmd}${err instanceof Error ? ` (${err.message})` : ""}`
          );
        }
    }
    this.tui.requestRender();
  }

  private applyServerCommands(commands: Record<string, unknown>[]): void {
    const mapped = commands
      .map((command) => {
        const name = typeof command.name === "string" ? command.name : "";
        if (!name) return null;
        return {
          name: name.startsWith("/") ? name : `/${name}`,
          description:
            typeof command.description === "string" ? command.description : "",
        };
      })
      .filter((command): command is { name: string; description: string } => command !== null);

    const merged = new Map<string, { name: string; description: string }>();
    for (const command of [...this.slashCommands, ...mapped]) {
      merged.set(command.name, command);
    }
    this.slashCommands = [...merged.values()];
    this.editor.setAutocompleteProvider(
      new CombinedAutocompleteProvider(this.slashCommands)
    );
  }

  private async switchSession(session: SessionInfo): Promise<void> {
    this.activeSessionId = session.id;

    this.chatLog.clearAll();

    if (this.statusLoader) {
      this.statusLoader.stop();
      this.statusContainer.removeChild(this.statusLoader);
      this.statusLoader = null;
    }

    const historyCount = await this.loadHistory(session.id);
    if (historyCount === 0) {
      this.chatLog.setEmptyState(this.buildEmptyState());
    }
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
      list.onSelect = async (item) => {
        try {
          await this.client.setSessionModel(this.activeSessionId, item.value);
          this.defaultModel = item.value;
          this.updateFooterModel(item.value);
          this.chatLog.addSystem(`Model: ${item.value}`);
        } catch (err) {
          this.chatLog.addSystem(
            `Failed to set model: ${err instanceof Error ? err.message : String(err)}`
          );
        }
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

  private buildEmptyState(): string[] {
    return [
      `${colors.accentBold("nanobot tui")}${colors.dim("  rose pine")}`,
      colors.dim(""),
      `${colors.text("Start a session below or use a command:")}`,
      `${colors.dim("/new")}${colors.border("  -  ")}${colors.dim("create a new session")}`,
      `${colors.dim("/sessions")}${colors.border("  -  ")}${colors.dim("switch sessions")}`,
      `${colors.dim("/model")}${colors.border("  -  ")}${colors.dim("select a model")}`,
      `${colors.dim("/compact")}${colors.border("  -  ")}${colors.dim("summarize current session")}`,
      colors.dim(""),
      `${colors.muted("Messages, tool runs, and replies will appear here in chronological order.")}`,
    ];
  }

  private syncFooterFromMessage(info: MessageInfo): void {
    if (info.providerID || info.modelID) {
      this.footer.setProviderModel(info.providerID || "", info.modelID || "");
    }

    if (info.mode) {
      this.footer.setThinkingLevel(info.mode);
    }

    if (info.tokens) {
      const used = info.tokens.input + info.tokens.output + info.tokens.reasoning;
      const total = this.currentContextLimit();
      const remaining = total > 0 ? Math.max(0, total - used) : 0;
      this.footer.setContextUsage(used, remaining);
    }
  }

  private updateFooterModel(fullModelId: string): void {
    if (!fullModelId) {
      this.footer.setProviderModel("", "");
      return;
    }
    const [provider, ...rest] = fullModelId.split("/");
    this.footer.setProviderModel(provider || "", rest.join("/") || fullModelId);
  }

  private currentContextLimit(): number {
    if (!this.providerCatalog) {
      return 0;
    }

    for (const provider of this.providerCatalog.providers) {
      for (const model of Object.values(provider.models)) {
        if (`${provider.id}/${model.id}` === this.defaultModel) {
          return model.limit.context;
        }
      }
    }

    return 0;
  }
}
