import { Container, Spacer, Text, type Component, type TUI } from "@mariozechner/pi-tui";

import type { MessageInfo, MessageWithParts, TextPart, ToolPart } from "../../api/types.js";
import { colors } from "../theme.js";
import { AssistantMessageComponent } from "./assistant-message.js";
import { ToolExecutionComponent } from "./tool-execution.js";
import { UserMessageComponent } from "./user-message.js";

type MessageMeta = {
  role: "user" | "assistant";
  created: number;
};

type EntryRecord = {
  order: number;
  seq: number;
  component: Component;
};

export class ChatLog extends Container {
  private readonly tui: TUI;
  private readonly messageMeta = new Map<string, MessageMeta>();
  private readonly assistantMessages = new Map<string, AssistantMessageComponent>();
  private readonly toolComponents = new Map<string, ToolExecutionComponent>();
  private readonly entries = new Map<string, EntryRecord>();
  private systemSeq = 0;
  private orderSeq = 0;

  constructor(tui: TUI) {
    super();
    this.tui = tui;
  }

  clearAll(): void {
    for (const tool of this.toolComponents.values()) {
      tool.stop();
    }

    this.clear();
    this.messageMeta.clear();
    this.assistantMessages.clear();
    this.toolComponents.clear();
    this.entries.clear();
    this.systemSeq = 0;
    this.orderSeq = 0;
  }

  addSystem(text: string): void {
    const key = `system:${this.systemSeq++}`;
    this.entries.set(key, {
      order: Number.MAX_SAFE_INTEGER - 1000 + this.systemSeq,
      seq: this.nextSeq(),
      component: new Text(colors.dim(text), 1, 0),
    });
    this.rebuild();
  }

  addHistoryMessage(message: MessageWithParts): void {
    this.upsertMessageInfo(message.info);

    for (const part of message.parts) {
      if (part.type === "text") {
        this.upsertTextPart(part);
      } else if (part.type === "tool") {
        this.upsertToolPart(part);
      }
    }
  }

  upsertMessageInfo(info: MessageInfo): void {
    const existing = this.messageMeta.get(info.id);
    this.messageMeta.set(info.id, {
      role: info.role,
      created: existing?.created ?? info.time.created,
    });
  }

  upsertTextPart(part: TextPart): void {
    const meta = this.messageMeta.get(part.messageID);
    if (!meta) {
      return;
    }

    if (meta.role === "user") {
      this.entries.set(`msg:${part.messageID}`, {
        order: meta.created,
        seq: this.nextSeq(),
        component: this.wrapWithSpacing(new UserMessageComponent(part.text).container),
      });
      this.rebuild();
      return;
    }

    let component = this.assistantMessages.get(part.messageID);
    if (!component) {
      component = new AssistantMessageComponent();
      this.assistantMessages.set(part.messageID, component);
    }

    component.updateContent(part.text);
    this.entries.set(`msg:${part.messageID}`, {
      order: meta.created,
      seq: this.entrySeq(`msg:${part.messageID}`),
      component: this.wrapWithSpacing(component.container),
    });
    this.rebuild();
  }

  upsertToolPart(part: ToolPart): void {
    let component = this.toolComponents.get(part.callID);
    if (!component) {
      component = new ToolExecutionComponent(this.tui, part.tool, part.state);
      this.toolComponents.set(part.callID, component);
    } else {
      component.update(part.state);
    }

    const order = part.state.time.start || this.messageMeta.get(part.messageID)?.created || 0;
    this.entries.set(`tool:${part.callID}`, {
      order,
      seq: this.entrySeq(`tool:${part.callID}`),
      component: this.wrapWithSpacing(component.container),
    });
    this.rebuild();
  }

  private wrapWithSpacing(component: Component): Component {
    const wrapper = new Container();
    wrapper.addChild(component);
    wrapper.addChild(new Spacer());
    return wrapper;
  }

  private entrySeq(key: string): number {
    const existing = this.entries.get(key);
    return existing?.seq ?? this.nextSeq();
  }

  private nextSeq(): number {
    this.orderSeq += 1;
    return this.orderSeq;
  }

  private rebuild(): void {
    this.clear();

    const ordered = [...this.entries.values()].sort((a, b) => {
      if (a.order !== b.order) {
        return a.order - b.order;
      }
      return a.seq - b.seq;
    });

    for (const entry of ordered) {
      this.addChild(entry.component);
    }
  }
}
