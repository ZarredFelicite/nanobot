import chalk from "chalk";
import type { MarkdownTheme, DefaultTextStyle } from "@mariozechner/pi-tui";
import type { SelectListTheme, EditorTheme } from "@mariozechner/pi-tui";

export const colors = {
  accent: chalk.cyan,
  accentBold: chalk.cyan.bold,
  dim: chalk.dim,
  dimBold: chalk.dim.bold,
  error: chalk.red,
  errorBold: chalk.red.bold,
  success: chalk.green,
  successBold: chalk.green.bold,
  warning: chalk.yellow,
  warningBold: chalk.yellow.bold,
  userLabel: chalk.blue.bold,
  assistantLabel: chalk.magenta.bold,
  toolName: chalk.yellow,
  toolRunning: chalk.yellow,
  toolDone: chalk.green,
  toolError: chalk.red,
  muted: chalk.gray,
  border: chalk.gray,
  statusBar: chalk.bgGray.white,
};

export const markdownTheme: MarkdownTheme = {
  heading: chalk.cyan.bold,
  link: chalk.blue.underline,
  linkUrl: chalk.dim,
  code: chalk.yellow,
  codeBlock: chalk.reset,
  codeBlockBorder: chalk.gray,
  quote: chalk.italic,
  quoteBorder: chalk.gray,
  hr: chalk.gray,
  listBullet: chalk.cyan,
  bold: chalk.bold,
  italic: chalk.italic,
  strikethrough: chalk.strikethrough,
  underline: chalk.underline,
};

export const defaultTextStyle: DefaultTextStyle = {
  color: chalk.reset,
};

export const selectListTheme: SelectListTheme = {
  selectedPrefix: chalk.cyan,
  selectedText: chalk.cyan.bold,
  description: chalk.dim,
  scrollInfo: chalk.dim,
  noMatch: chalk.dim.italic,
};

export const editorTheme: EditorTheme = {
  borderColor: chalk.cyan,
  selectList: selectListTheme,
};
