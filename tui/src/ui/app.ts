import {
  TUI,
  ProcessTerminal,
  Container,
  Text,
  Loader,
  Spacer,
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
  MessageWithParts,
  TextPart,
  ToolPart,
  PermissionRequest,
  ProviderCatalog,
} from "../api/types.js";

import { editorTheme, colors } from "./theme.js";
import { UserMessageComponent } from "./components/user-message.js";
import { AssistantMessageComponent } from "./components/assistant-message.js";
import { ToolExecutionComponent } from "./components/tool-execution.js";
import { FooterComponent } from "./components/footer.js";
import { PermissionDialogComponent } from "./components/permission-dialog.js";
import { SessionSelectorComponent } from "./components/session-selector.js";

export class App {
  private client: NanobotClient;
  private tui: TUI;
  private terminal: ProcessTerminal;
  private sse: SSEConnection;

  // Layout containers
  private chatContainer: Container;
  private statusContainer: Container;
  private editorContainer: Container;

  // Components
  private footer: FooterComponent;
  private editor: Editor;
  private statusLoader: Loader | null = null;
  private overlay: OverlayHandle | null = null;

  // State
  private activeSessionId = "";
  private isBusy = false;
  private defaultModel = "";
  private assistantMessages = new Map<string, AssistantMessageComponent>();
  private toolComponents = new Map<string, ToolExecutionComponent>();
  // Track which components have been added to chat, keyed by message/tool ID
  private addedComponents = new Set<string>();

  constructor(client: NanobotClient) {
    this.client = client;
    this.terminal = new ProcessTerminal();
    this.tui = new TUI(this.terminal);

    // Layout
    this.chatContainer = new Container();
    this.statusContainer = new Container();
    this.editorContainer = new Container();
    this.footer = new FooterComponent();

    this.tui.addChild(this.chatContainer);
    this.tui.addChild(this.statusContainer);
    this.tui.addChild(this.editorContainer);
    this.tui.addChild(this.footer.container);

    // Editor
    this.editor = new Editor(this.tui, editorTheme, { paddingX: 1 });
    this.editor.onSubmit = (text) => this.handleSubmit(text);
    this.editorContainer.addChild(this.editor);

    // Autocomplete with slash commands
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

    // SSE
    this.sse = new SSEConnection(
      client.getEventUrl(),
      (event) => this.handleSSEEvent(event)
    );

    // Global keybindings
    this.tui.addInputListener((data) => {
      // If overlay is showing, route input there
      if (this.overlay) return undefined;

      if (matchesKey(data, Key.ctrl("c"))) {
        if (this.isBusy) {
          this.client.abortSession(this.activeSessionId).catch(() => {});
          return { consume: true };
        } else if (this.editor.getText().trim() === "") {
          this.shutdown();
          return { consume: true };
        }
      }

      if (matchesKey(data, Key.escape)) {
        if (this.isBusy) {
          this.client.abortSession(this.activeSessionId).catch(() => {});
          return { consume: true };
        }
      }

      return undefined;
    });
  }

  async start(): Promise<void> {
    this.tui.start();

    // Show connecting message
    const connecting = new Text(colors.dim("Connecting to nanobot..."), 1, 0);
    this.chatContainer.addChild(connecting);
    this.tui.requestRender();

    try {
      // Bootstrap
      const [providers, sessions] = await Promise.all([
        this.client.getProviders(),
        this.client.listSessions(),
      ]);

      this.defaultModel = providers.defaultModel;
      this.footer.setModel(this.defaultModel);

      // Use most recent session or create new one
      let session: SessionInfo;
      if (sessions.length > 0) {
        session = sessions.sort(
          (a, b) => b.time.updated - a.time.updated
        )[0];
      } else {
        session = await this.client.createSession();
      }

      this.activeSessionId = session.id;
      this.footer.setSession(session.title || session.id);

      // Load message history
      await this.loadHistory(session.id);

      // Remove connecting message
      this.chatContainer.removeChild(connecting);

      // Connect SSE
      this.sse.connect();

      // Focus editor
      this.tui.setFocus(this.editor);
      this.tui.requestRender();
    } catch (err) {
      this.chatContainer.removeChild(connecting);
      this.chatContainer.addChild(
        new Text(
          colors.error(
            `Failed to connect: ${err instanceof Error ? err.message : String(err)}`
          ),
          1,
          0
        )
      );
      this.tui.requestRender();
    }
  }

  private async loadHistory(sessionId: string): Promise<void> {
    try {
      const messages = await this.client.getMessages(sessionId);
      for (const msg of messages) {
        this.renderHistoryMessage(msg);
      }
    } catch {
      // No history or error — that's fine
    }
  }

  private renderHistoryMessage(msg: MessageWithParts): void {
    if (msg.info.role === "user") {
      const textPart = msg.parts.find((p) => p.type === "text") as TextPart | undefined;
      if (textPart) {
        const userComp = new UserMessageComponent(textPart.text);
        this.chatContainer.addChild(userComp.container);
        this.chatContainer.addChild(new Spacer());
      }
    } else {
      // Assistant message — render text and tool parts
      for (const part of msg.parts) {
        if (part.type === "text") {
          const comp = new AssistantMessageComponent();
          comp.updateContent((part as TextPart).text);
          this.chatContainer.addChild(comp.container);
        } else if (part.type === "tool") {
          const tp = part as ToolPart;
          const comp = new ToolExecutionComponent(this.tui, tp.tool, tp.state);
          this.chatContainer.addChild(comp.container);
        }
      }
      this.chatContainer.addChild(new Spacer());
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
        this.handlePartUpdated(event.properties.part, event.properties.delta);
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
    } else {
      if (this.statusLoader) {
        this.statusLoader.stop();
        this.statusContainer.removeChild(this.statusLoader);
        this.statusLoader = null;
      }
    }

    // Update token count from context
    if (status.context) {
      const tokens = status.context.tokens as
        | { used?: number }
        | undefined;
      if (tokens?.used) {
        this.footer.setTokens(tokens.used);
      }
    }
  }

  private handleMessageUpdated(_info: MessageInfo): void {
    // Nothing to render here for now — parts carry the content
  }

  private handlePartUpdated(part: MessagePart, delta?: string): void {
    if (part.type === "text") {
      this.handleTextPart(part as TextPart, delta);
    } else if (part.type === "tool") {
      this.handleToolPart(part as ToolPart);
    }
  }

  private handleTextPart(part: TextPart, delta?: string): void {
    const msgId = part.messageID;
    let comp = this.assistantMessages.get(msgId);

    if (!comp) {
      comp = new AssistantMessageComponent();
      this.assistantMessages.set(msgId, comp);
      this.chatContainer.addChild(comp.container);
      this.addedComponents.add(msgId);
    }

    // Use accumulated text from the part
    comp.updateContent(part.text);
  }

  private handleToolPart(part: ToolPart): void {
    const callId = part.callID;
    let comp = this.toolComponents.get(callId);

    if (!comp) {
      comp = new ToolExecutionComponent(this.tui, part.tool, part.state);
      this.toolComponents.set(callId, comp);
      this.chatContainer.addChild(comp.container);
      this.addedComponents.add(callId);
    } else {
      const oldContainer = comp.update(part.state);
      if (oldContainer !== comp.container) {
        // Component was replaced — swap in the chat
        const children = this.chatContainer.children;
        const idx = children.indexOf(oldContainer);
        if (idx >= 0) {
          this.chatContainer.removeChild(oldContainer);
          // Re-add at roughly the same position
          this.chatContainer.addChild(comp.container);
        }
      }
    }
  }

  private showPermissionDialog(request: PermissionRequest): void {
    const dialog = new PermissionDialogComponent(request);
    dialog.onReply = async (requestId, reply) => {
      try {
        await this.client.replyPermission(requestId, reply);
      } catch (err) {
        // Show error briefly
      }
      this.overlay?.hide();
      this.overlay = null;
      this.tui.setFocus(this.editor);
    };

    this.overlay = this.tui.showOverlay(dialog.container, {
      anchor: "center",
      width: "60%",
      maxHeight: "40%",
    });
  }

  private async handleSubmit(text: string): Promise<void> {
    const trimmed = text.trim();
    if (!trimmed) return;

    this.editor.addToHistory(trimmed);

    // Slash commands
    if (trimmed.startsWith("/")) {
      await this.handleSlashCommand(trimmed);
      return;
    }

    // Add user message to chat
    const userMsg = new UserMessageComponent(trimmed);
    this.chatContainer.addChild(userMsg.container);
    this.chatContainer.addChild(new Spacer());
    this.tui.requestRender();

    // Send to API
    try {
      await this.client.sendMessage(this.activeSessionId, trimmed);
    } catch (err) {
      this.chatContainer.addChild(
        new Text(
          colors.error(
            `Send failed: ${err instanceof Error ? err.message : String(err)}`
          ),
          1,
          0
        )
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
          this.chatContainer.addChild(
            new Text(colors.success(`Model set to ${modelName}`), 1, 0)
          );
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
        this.chatContainer.addChild(
          new Text(colors.success("Session summarized"), 1, 0)
        );
        break;
      }

      default:
        this.chatContainer.addChild(
          new Text(colors.warning(`Unknown command: ${cmd}`), 1, 0)
        );
    }
    this.tui.requestRender();
  }

  private async switchSession(session: SessionInfo): Promise<void> {
    this.activeSessionId = session.id;
    this.footer.setSession(session.title || session.id);

    // Clear chat
    this.chatContainer.clear();
    this.assistantMessages.clear();
    this.toolComponents.clear();
    this.addedComponents.clear();

    // Stop running loaders
    if (this.statusLoader) {
      this.statusLoader.stop();
      this.statusContainer.removeChild(this.statusLoader);
      this.statusLoader = null;
    }

    // Load history
    await this.loadHistory(session.id);
    this.tui.requestRender();
  }

  private async showSessionSelector(): Promise<void> {
    try {
      const sessions = await this.client.listSessions();
      if (sessions.length === 0) {
        this.chatContainer.addChild(
          new Text(colors.dim("No sessions found"), 1, 0)
        );
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

      this.overlay = this.tui.showOverlay(selector.container, {
        anchor: "center",
        width: "70%",
        maxHeight: "60%",
      });
    } catch (err) {
      this.chatContainer.addChild(
        new Text(colors.error(`Failed to list sessions: ${err}`), 1, 0)
      );
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

      const container = new Container();
      const { SelectList } = await import("@mariozechner/pi-tui");
      const { selectListTheme } = await import("./theme.js");

      const list = new SelectList(items, 10, selectListTheme);
      list.onSelect = (item) => {
        this.defaultModel = item.value;
        this.footer.setModel(item.value);
        this.chatContainer.addChild(
          new Text(colors.success(`Model: ${item.value}`), 1, 0)
        );
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

      container.addChild(list);
      this.overlay = this.tui.showOverlay(container, {
        anchor: "center",
        width: "60%",
        maxHeight: "50%",
      });
    } catch (err) {
      this.chatContainer.addChild(
        new Text(colors.error(`Failed to list models: ${err}`), 1, 0)
      );
      this.tui.requestRender();
    }
  }

  private shutdown(): void {
    // Stop all tool loaders
    for (const comp of this.toolComponents.values()) {
      comp.stop();
    }
    if (this.statusLoader) this.statusLoader.stop();

    this.sse.disconnect();
    this.tui.stop();
    process.exit(0);
  }
}
