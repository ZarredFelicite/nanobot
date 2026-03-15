import type { Component } from "@mariozechner/pi-tui";
import { colors } from "../theme.js";

export class UserMessageComponent implements Component {
  readonly container: Component;
  private readonly lines: string[];

  constructor(text: string) {
    this.lines = text.split("\n");
    this.container = this;
  }

  handleInput(): void {}

  invalidate(): void {}

  render(width: number): string[] {
    const lineWidth = Math.max(1, width);
    return this.lines.map((line) => {
      const trimmed = line.slice(0, lineWidth);
      const padded = trimmed.padEnd(lineWidth, " ");
      return colors.userBackground(colors.userText(padded));
    });
  }
}
