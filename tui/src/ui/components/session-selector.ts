import { Container, SelectList, type Component } from "@mariozechner/pi-tui";
import { selectListTheme } from "../theme.js";
import type { SessionInfo } from "../../api/types.js";

export class SessionSelectorComponent implements Component {
  readonly container: Container;
  private selectList: SelectList;
  onSelect?: (session: SessionInfo) => void;
  onCancel?: () => void;

  constructor(sessions: SessionInfo[]) {
    this.container = new Container();

    const items = sessions.map((s) => ({
      value: s.id,
      label: s.title || s.id,
      description: new Date(s.time.updated).toLocaleString(),
    }));

    this.selectList = new SelectList(items, 10, selectListTheme);

    this.selectList.onSelect = (item) => {
      const session = sessions.find((s) => s.id === item.value);
      if (session) this.onSelect?.(session);
    };

    this.selectList.onCancel = () => {
      this.onCancel?.();
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
