import { Text } from "@mariozechner/pi-tui";
import { colors } from "../theme.js";

export class FooterComponent {
  readonly container: Text;
  private model = "";
  private sessionTitle = "";
  private busy = false;
  private tokenCount = 0;

  constructor() {
    this.container = new Text("", 0, 0, colors.statusBar);
    this.render();
  }

  setModel(model: string): void {
    this.model = model;
    this.render();
  }

  setSession(title: string): void {
    this.sessionTitle = title;
    this.render();
  }

  setBusy(busy: boolean): void {
    this.busy = busy;
    this.render();
  }

  setTokens(count: number): void {
    this.tokenCount = count;
    this.render();
  }

  private render(): void {
    const status = this.busy ? " ● busy" : " ○ idle";
    const model = this.model ? ` ${this.model}` : "";
    const session = this.sessionTitle ? ` │ ${this.sessionTitle}` : "";
    const tokens = this.tokenCount > 0 ? ` │ ${this.tokenCount} tokens` : "";
    this.container.setText(`${status}${model}${session}${tokens}`);
  }
}
