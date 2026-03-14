import { Text, Loader, type TUI } from "@mariozechner/pi-tui";
import { colors } from "../theme.js";
import type { ToolState } from "../../api/types.js";

export class ToolExecutionComponent {
  container: Text | Loader;
  private tui: TUI;
  private toolName: string;

  constructor(tui: TUI, toolName: string, state: ToolState) {
    this.tui = tui;
    this.toolName = toolName;

    if (state.status === "running") {
      const loader = new Loader(
        tui,
        colors.toolRunning,
        colors.dim,
        this.formatTitle(state)
      );
      loader.start();
      this.container = loader;
    } else {
      this.container = new Text(this.formatCompleted(state), 1, 0);
    }
  }

  update(state: ToolState): Text | Loader {
    const oldContainer = this.container;

    if (state.status === "running") {
      if (this.container instanceof Loader) {
        this.container.setMessage(this.formatTitle(state));
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
        this.formatTitle(state)
      );
      loader.start();
      this.container = loader;
    } else {
      this.container = new Text(this.formatCompleted(state), 1, 0);
    }

    return oldContainer;
  }

  stop(): void {
    if (this.container instanceof Loader) {
      this.container.stop();
    }
  }

  private formatTitle(state: ToolState): string {
    if (state.title) return `${colors.toolName(this.toolName)} ${state.title}`;
    const input = state.input;
    if (input.command) return `${colors.toolName(this.toolName)}  ${colors.dim(input.command)}`;
    if (input.filePath) return `${colors.toolName(this.toolName)}  ${colors.dim(input.filePath)}`;
    if (input.pattern) return `${colors.toolName(this.toolName)}  ${colors.dim(input.pattern)}`;
    if (input.query) return `${colors.toolName(this.toolName)}  ${colors.dim(input.query)}`;
    if (input.url) return `${colors.toolName(this.toolName)}  ${colors.dim(input.url)}`;
    return colors.toolName(this.toolName);
  }

  private formatCompleted(state: ToolState): string {
    const icon =
      state.status === "error"
        ? colors.toolError("✗")
        : colors.toolDone("✓");
    const name = colors.toolName(this.toolName);
    const title = state.title || "";

    if (state.status === "error") {
      const err = state.error || "unknown error";
      return `${icon} ${name} ${title}\n  ${colors.error(err)}`;
    }

    // Show brief output preview
    let preview = "";
    if (state.output) {
      const lines = state.output.split("\n").filter((l) => l.trim());
      if (lines.length > 0) {
        const shown = lines.slice(0, 3).join("\n  ");
        const more = lines.length > 3 ? colors.dim(` (+${lines.length - 3} lines)`) : "";
        preview = `\n  ${colors.dim(shown)}${more}`;
      }
    }

    return `${icon} ${name} ${title}${preview}`;
  }
}
