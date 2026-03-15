import type { Component } from "@mariozechner/pi-tui";
import { colors } from "../theme.js";

export class FooterComponent implements Component {
  private provider = "";
  private model = "";
  private thinkingLevel = "auto";
  private contextUsed = 0;
  private contextRemaining = 0;

  setProviderModel(provider: string, model: string): void {
    this.provider = provider;
    this.model = model;
  }

  setThinkingLevel(level: string): void {
    this.thinkingLevel = level;
  }

  setContextUsage(used: number, remaining: number): void {
    this.contextUsed = used;
    this.contextRemaining = remaining;
  }

  handleInput(): void {}

  invalidate(): void {}

  render(width: number): string[] {
    const total = this.contextUsed + this.contextRemaining;
    const percent = total > 0 ? ((this.contextUsed / total) * 100).toFixed(1) : "0.0";
    const leftParts = [colors.dim(`${percent}%/${formatCompact(total)}`)];

    const rightParts = [
      this.provider ? colors.muted(`(${this.provider})`) : "",
      this.model ? colors.dim(this.model) : "",
      colors.dim(`• ${normalizeThinking(this.thinkingLevel)}`),
    ].filter(Boolean);

    const left = leftParts.join(colors.border(" | "));
    const right = rightParts.join(colors.border(" | "));
    const gap = Math.max(1, width - stripAnsi(left).length - stripAnsi(right).length);
    return [`${left}${" ".repeat(gap)}${right}`];
  }
}

function stripAnsi(value: string): string {
  return value.replace(/\x1B\[[0-9;]*m/g, "");
}

function formatCompact(value: number): string {
  if (value >= 1000) {
    return `${(value / 1000).toFixed(0)}k`;
  }
  return String(value);
}

function normalizeThinking(value: string): string {
  if (!value || value === "default") {
    return "auto";
  }
  return value;
}
