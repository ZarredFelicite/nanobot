import { Text, Loader, type TUI } from "@mariozechner/pi-tui";
import { colors } from "../theme.js";
import type { ToolState } from "../../api/types.js";

export class ToolExecutionComponent {
  container: Text | Loader;
  private tui: TUI;
  private toolName: string;
  private lastState: ToolState;
  private _expanded = false;
  private _selected = false;

  constructor(tui: TUI, toolName: string, state: ToolState) {
    this.tui = tui;
    this.toolName = toolName;
    this.lastState = state;

    if (state.status === "running") {
      const loader = new Loader(
        tui,
        colors.toolRunning,
        colors.dim,
        this.withIndent(this.formatTitle(state))
      );
      loader.start();
      this.container = loader;
    } else {
      this.container = new Text(this.formatCompleted(state), 3, 0);
    }
  }

  get expanded(): boolean {
    return this._expanded;
  }

  get selected(): boolean {
    return this._selected;
  }

  set selected(value: boolean) {
    if (this._selected === value) return;
    this._selected = value;
    this.refreshContainer();
  }

  toggleExpanded(): void {
    if (this.lastState.status === "running") return;
    this._expanded = !this._expanded;
    this.refreshContainer();
  }

  /** Re-create the Text container to reflect current selected/expanded state. */
  refreshContainer(): void {
    if (this.container instanceof Loader) return;
    this.container = new Text(this.formatCompleted(this.lastState), 3, 0);
  }

  update(state: ToolState): Text | Loader {
    const oldContainer = this.container;
    this.lastState = state;

    if (state.status === "running") {
      if (this.container instanceof Loader) {
        this.container.setMessage(this.withIndent(this.formatTitle(state)));
        return oldContainer;
      }
    }

    // Status changed — replace the component
    if (this.container instanceof Loader) {
      this.container.stop();
    }

    if (state.status === "running") {
      const loader = new Loader(
        this.tui,
        colors.toolRunning,
        colors.dim,
        this.withIndent(this.formatTitle(state))
      );
      loader.start();
      this.container = loader;
    } else {
      this.container = new Text(this.formatCompleted(state), 3, 0);
    }

    return oldContainer;
  }

  stop(): void {
    if (this.container instanceof Loader) {
      this.container.stop();
    }
  }

  private formatTitle(state: ToolState): string {
    if (state.title) return `${colors.toolLabel("tool")} ${colors.toolName(this.toolName)} ${colors.dim(state.title)}`;
    const input = state.input;
    if (input.command) return `${colors.toolLabel("tool")} ${colors.toolName(this.toolName)} ${colors.dim(input.command)}`;
    if (input.filePath) return `${colors.toolLabel("tool")} ${colors.toolName(this.toolName)} ${colors.dim(input.filePath)}`;
    if (input.pattern) return `${colors.toolLabel("tool")} ${colors.toolName(this.toolName)} ${colors.dim(input.pattern)}`;
    if (input.query) return `${colors.toolLabel("tool")} ${colors.toolName(this.toolName)} ${colors.dim(input.query)}`;
    if (input.url) return `${colors.toolLabel("tool")} ${colors.toolName(this.toolName)} ${colors.dim(input.url)}`;
    return `${colors.toolLabel("tool")} ${colors.toolName(this.toolName)}`;
  }

  /** Line 1 summary: title or empty */
  private toolSummary(state: ToolState): string {
    return state.title || "";
  }

  /** Line 2 detail: raw command/path/pattern */
  private toolCommand(state: ToolState): string {
    const input = state.input;
    return input.command || input.filePath || input.pattern || input.query || input.url || "";
  }

  private outputLineCount(state: ToolState): number {
    if (!state.output) return 0;
    return state.output.split("\n").filter((l) => l.trim()).length;
  }

  private formatCompleted(state: ToolState): string {
    const icon =
      state.status === "error" ? colors.toolError("x") : colors.toolDone("+");
    const name = colors.toolName(this.toolName);
    const selectIndicator = this._selected ? colors.accent("> ") : "  ";
    const summary = this.toolSummary(state);
    const command = this.toolCommand(state);
    const lineCount = this.outputLineCount(state);
    const countLabel = lineCount > 0 ? colors.dim(` [${lineCount}]`) : "";

    // Line 1: icon toolName - summary [lines]
    const summaryPart = summary ? ` - ${colors.dim(summary)}` : "";
    const line1 = `${selectIndicator}${icon} ${name}${summaryPart}${countLabel}`;

    if (state.status === "error") {
      const errorLabel = colors.error("[error]");
      const line1err = `${selectIndicator}${icon} ${name}${summaryPart} ${errorLabel}`;
      const line2 = command ? `\n     ${colors.dim(command)}` : "";
      if (this._expanded) {
        const err = state.error || "unknown error";
        return `${line1err}${line2}\n  ${colors.error(err)}`;
      }
      return `${line1err}${line2}`;
    }

    // Line 2: command/path (indented)
    const line2 = command ? `\n     ${colors.dim(command)}` : "";

    if (this._expanded && state.output) {
      const lines = state.output.split("\n").filter((l) => l.trim());
      const shown = lines.join(`\n  `);
      return `${line1}${line2}\n  ${colors.toolOutput(shown)}`;
    }

    return `${line1}${line2}`;
  }

  private withIndent(text: string): string {
    return `  ${text}`;
  }
}
