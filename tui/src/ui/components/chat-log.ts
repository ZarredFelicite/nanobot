import { Container, Spacer, Text, type Component, type TUI } from "@mariozechner/pi-tui";

import type { MessageInfo, MessageWithParts, TextPart, ToolPart } from "../../api/types.js";
import { colors } from "../theme.js";
import { AssistantMessageComponent } from "./assistant-message.js";
import { ToolExecutionComponent } from "./tool-execution.js";
import { UserMessageComponent } from "./user-message.js";

type MessageMeta = {
  role: "user" | "assistant";
  created: number;
  mode?: string;
};

type EntryRecord = {
  order: number;
  seq: number;
  component: Component;
  /** Key into toolComponents if this entry is a tool block */
  toolCallID?: string;
};

export class ChatLog extends Container {
  private readonly tui: TUI;
  private readonly messageMeta = new Map<string, MessageMeta>();
  private readonly assistantMessages = new Map<string, AssistantMessageComponent>();
  private readonly toolComponents = new Map<string, ToolExecutionComponent>();
  private readonly entries = new Map<string, EntryRecord>();
  private emptyState: string[] = [];
  private systemSeq = 0;
  private orderSeq = 0;

  /** Ordered list of entry keys that can be selected (tool blocks) */
  private selectableKeys: string[] = [];
  /** Index into selectableKeys, -1 = nothing selected */
  private selectedIndex = -1;

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
    this.emptyState = [];
    this.systemSeq = 0;
    this.orderSeq = 0;
    this.selectableKeys = [];
    this.selectedIndex = -1;
  }

  setEmptyState(lines: string[]): void {
    this.emptyState = lines;
    this.rebuild();
  }

  clearEmptyState(): void {
    if (this.emptyState.length === 0) {
      return;
    }
    this.emptyState = [];
    this.rebuild();
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
      mode: info.mode ?? existing?.mode,
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

    if (meta.mode === "compact") {
      const partKey = `part:${part.id}`;
      const created = part.time.created || meta.created;
      const label = part.text || "Context compacted";
      const line = colors.warning("─".repeat(3));
      const text = `${line} ${colors.warningBold("⊟ " + label)} ${line}`;
      this.entries.set(partKey, {
        order: created,
        seq: this.entrySeq(partKey),
        component: this.wrapWithSpacing(new Text(text, 1, 0)),
      });
      this.rebuild();
      return;
    }

    const created = part.time.created || meta.created;
    const partKey = `part:${part.id}`;
    const variant = part.phase === "thinking" ? "thinking" : "assistant";
    let component = this.assistantMessages.get(part.id);
    if (!component) {
      component = new AssistantMessageComponent(variant);
      this.assistantMessages.set(part.id, component);
    }

    component.updateContent(part.text);
    this.entries.set(partKey, {
      order: created,
      seq: this.entrySeq(partKey),
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

    const entryKey = `tool:${part.callID}`;
    const order = part.state.time.start || this.messageMeta.get(part.messageID)?.created || 0;
    this.entries.set(entryKey, {
      order,
      seq: this.entrySeq(entryKey),
      component: this.wrapWithSpacing(component.container),
      toolCallID: part.callID,
    });
    this.rebuild();
  }

  /** Move selection to the previous selectable block (toward top). Returns true if handled. */
  selectPrevious(): boolean {
    if (this.selectableKeys.length === 0) return false;
    if (this.selectedIndex < 0) {
      // First activation: start at the last (bottom) tool
      this.setSelection(this.selectableKeys.length - 1);
    } else if (this.selectedIndex > 0) {
      this.setSelection(this.selectedIndex - 1);
    }
    return true;
  }

  /** Move selection to the next selectable block (toward bottom). Returns true if handled. */
  selectNext(): boolean {
    if (this.selectableKeys.length === 0) return false;
    if (this.selectedIndex < 0) {
      // First activation: start at the last (bottom) tool
      this.setSelection(this.selectableKeys.length - 1);
    } else if (this.selectedIndex < this.selectableKeys.length - 1) {
      this.setSelection(this.selectedIndex + 1);
    }
    return true;
  }

  /** Clear block selection. */
  clearSelection(): void {
    if (this.selectedIndex >= 0) {
      const key = this.selectableKeys[this.selectedIndex];
      this.rewrapToolEntry(key, (tool) => { tool.selected = false; });
      this.selectedIndex = -1;
      this.rebuild();
      this.tui.requestRender();
    }
  }

  /** Expand the currently selected block. Returns true if handled. */
  expandSelected(): boolean {
    return this.setSelectedExpanded(true);
  }

  /** Collapse the currently selected block. Returns true if handled. */
  collapseSelected(): boolean {
    return this.setSelectedExpanded(false);
  }

  private setSelectedExpanded(expanded: boolean): boolean {
    if (this.selectedIndex < 0) return false;
    const key = this.selectableKeys[this.selectedIndex];

    const entry = this.entries.get(key);
    if (!entry?.toolCallID) return false;
    const tool = this.toolComponents.get(entry.toolCallID);
    if (!tool || tool.expanded === expanded) return false;

    tool.toggleExpanded();
    entry.component = this.wrapWithSpacing(tool.container);
    this.rebuild();
    this.tui.requestRender();
    return true;
  }

  private setSelection(index: number): void {
    // Deselect previous
    if (this.selectedIndex >= 0 && this.selectedIndex < this.selectableKeys.length) {
      const prevKey = this.selectableKeys[this.selectedIndex];
      this.rewrapToolEntry(prevKey, (tool) => { tool.selected = false; });
    }

    this.selectedIndex = index;

    // Select new
    if (index >= 0 && index < this.selectableKeys.length) {
      const key = this.selectableKeys[index];
      this.rewrapToolEntry(key, (tool) => { tool.selected = true; });
    }

    this.rebuild();
    this.tui.requestRender();
  }

  /** Mutate a tool component and re-wrap its entry in the entries map. */
  private rewrapToolEntry(entryKey: string, mutate: (tool: ToolExecutionComponent) => void): void {
    const entry = this.entries.get(entryKey);
    if (!entry?.toolCallID) return;
    const tool = this.toolComponents.get(entry.toolCallID);
    if (!tool) return;

    mutate(tool);
    entry.component = this.wrapWithSpacing(tool.container);
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

    if (this.entries.size === 0 && this.emptyState.length > 0) {
      this.addChild(new Text(this.emptyState.join("\n"), 1, 0));
      this.addChild(new Spacer());
    }

    const orderedEntries = [...this.entries.entries()].sort((a, b) => {
      if (a[1].order !== b[1].order) {
        return a[1].order - b[1].order;
      }
      return a[1].seq - b[1].seq;
    });

    // Rebuild selectable keys in display order
    const newSelectableKeys: string[] = [];
    for (const [key, entry] of orderedEntries) {
      if (entry.toolCallID) {
        newSelectableKeys.push(key);
      }
    }
    this.selectableKeys = newSelectableKeys;

    // Clamp selection index
    if (this.selectedIndex >= this.selectableKeys.length) {
      this.selectedIndex = this.selectableKeys.length - 1;
    }

    const activeKey = this.selectedIndex >= 0
      ? this.selectableKeys[this.selectedIndex]
      : undefined;

    if (!activeKey) {
      // No selection — render everything normally
      for (const [, entry] of orderedEntries) {
        this.addChild(entry.component);
      }
      return;
    }

    // Selection active — render a window of entries around the selected block,
    // filling roughly half the screen above and half below, so the selected
    // block appears vertically centered in the viewport.
    const termWidth = this.tui.terminal.columns;
    const termHeight = this.tui.terminal.rows;
    const halfScreen = Math.floor(termHeight / 2);

    // Find the index of the selected entry in orderedEntries
    const selectedIdx = orderedEntries.findIndex(([key]) => key === activeKey);
    if (selectedIdx < 0) {
      for (const [, entry] of orderedEntries) {
        this.addChild(entry.component);
      }
      return;
    }

    // Collect entries above (walk backwards from selected, fill half screen)
    // Skip any entry that would overflow the budget
    const aboveEntries: Component[] = [];
    let linesAbove = 0;
    for (let i = selectedIdx - 1; i >= 0 && linesAbove < halfScreen; i--) {
      const comp = orderedEntries[i][1].component;
      const lines = comp.render(termWidth).length;
      if (linesAbove + lines > halfScreen) break;
      aboveEntries.unshift(comp);
      linesAbove += lines;
    }

    // Collect entries below (walk forward from selected, fill half screen)
    // Skip any entry that would overflow the budget
    const belowEntries: Component[] = [];
    let linesBelow = 0;
    for (let i = selectedIdx + 1; i < orderedEntries.length && linesBelow < halfScreen; i++) {
      const comp = orderedEntries[i][1].component;
      const lines = comp.render(termWidth).length;
      if (linesBelow + lines > halfScreen) break;
      belowEntries.push(comp);
      linesBelow += lines;
    }

    // Render: above entries, selected entry, below entries, padding
    for (const comp of aboveEntries) {
      this.addChild(comp);
    }
    this.addChild(orderedEntries[selectedIdx][1].component);
    for (const comp of belowEntries) {
      this.addChild(comp);
    }

    const paddingNeeded = Math.max(0, halfScreen - linesBelow);
    if (paddingNeeded > 0) {
      this.addChild(new Spacer(paddingNeeded));
    }
  }
}
