import { Container, Text, SelectList, type Component } from "@mariozechner/pi-tui";
import { colors, selectListTheme } from "../theme.js";
import type { PermissionRequest, PermissionReply } from "../../api/types.js";

export class PermissionDialogComponent implements Component {
  readonly container: Container;
  private selectList: SelectList;
  onReply?: (requestId: string, reply: PermissionReply) => void;

  constructor(request: PermissionRequest) {
    this.container = new Container();

    // Description
    const desc = new Text(
      `${colors.warningBold("Permission Required")}\n` +
        `${colors.toolName(request.permission)}: ${request.patterns.join(", ")}`,
      1,
      1
    );
    this.container.addChild(desc);

    // Options
    this.selectList = new SelectList(
      [
        { value: "once", label: "Allow once" },
        { value: "always", label: "Allow always" },
        { value: "reject", label: "Reject" },
      ],
      3,
      selectListTheme
    );

    this.selectList.onSelect = (item) => {
      this.onReply?.(request.id, item.value as PermissionReply);
    };

    this.container.addChild(this.selectList);
  }

  handleInput(data: string): void {
    this.selectList.handleInput(data);
  }

  render(width: number): string[] {
    return this.container.render(width);
  }

  invalidate(): void {
    this.container.invalidate();
  }
}
