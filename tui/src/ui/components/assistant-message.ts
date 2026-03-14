import { Markdown } from "@mariozechner/pi-tui";
import { markdownTheme, defaultTextStyle } from "../theme.js";

export class AssistantMessageComponent {
  readonly container: Markdown;
  private text = "";

  constructor() {
    this.container = new Markdown("", 1, 0, markdownTheme, defaultTextStyle);
  }

  updateContent(text: string): void {
    this.text = text;
    this.container.setText(text);
  }

  appendDelta(delta: string): void {
    this.text += delta;
    this.container.setText(this.text);
  }

  getText(): string {
    return this.text;
  }
}
