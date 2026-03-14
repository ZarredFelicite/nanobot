import { Text } from "@mariozechner/pi-tui";
import { colors } from "../theme.js";

export class UserMessageComponent {
  readonly container: Text;

  constructor(text: string) {
    this.container = new Text(
      `${colors.userLabel("❯")} ${text}`,
      1,
      0
    );
  }
}
