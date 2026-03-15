import { Markdown } from "@mariozechner/pi-tui";
import { markdownTheme, defaultTextStyle, thinkingTextStyle } from "../theme.js";

type AssistantMessageVariant = "assistant" | "thinking";

export class AssistantMessageComponent {
  readonly container: Markdown;
  private text = "";

  constructor(variant: AssistantMessageVariant = "assistant") {
    this.container = new Markdown(
      "",
      3,
      0,
      markdownTheme,
      variant === "thinking" ? thinkingTextStyle : defaultTextStyle
    );
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
