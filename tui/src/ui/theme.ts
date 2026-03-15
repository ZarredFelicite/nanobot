import chalk from "chalk";
import type { MarkdownTheme, DefaultTextStyle } from "@mariozechner/pi-tui";
import type { SelectListTheme, EditorTheme } from "@mariozechner/pi-tui";

export interface ThemeOverrides {
  accent?: string;
  border?: string;
  userBackground?: string;
  userText?: string;
  assistantText?: string;
  headingText?: string;
  thinkingText?: string;
  toolName?: string;
  toolOutput?: string;
  code?: string;
  codeBlock?: string;
}

const themeState = {
  accent: "#9ccfd8",
  border: "#524f67",
  userBackground: "#1f1d2e",
  userText: "#c4a7e7",
  assistantText: "#9ccfd8",
  headingText: "#e0def4",
  thinkingText: "#908caa",
  toolName: "#ebbcba",
  toolOutput: "#524f67",
  code: "#ebbcba",
  codeBlock: "#31748f",
};

export const colors = {
  text: chalk.hex("#e0def4"),
  accent: chalk.hex("#9ccfd8"),
  accentBold: chalk.hex("#9ccfd8").bold,
  dim: chalk.hex("#908caa"),
  dimBold: chalk.hex("#908caa").bold,
  muted: chalk.hex("#6e6a86"),
  border: chalk.hex("#524f67"),
  subtle: chalk.hex("#393552"),
  error: chalk.hex("#eb6f92"),
  errorBold: chalk.hex("#eb6f92").bold,
  success: chalk.hex("#31748f"),
  successBold: chalk.hex("#31748f").bold,
  warning: chalk.hex("#ebbcba"),
  warningBold: chalk.hex("#ebbcba").bold,
  rose: chalk.hex("#ebbcba"),
  userBackground: chalk.bgHex("#1f1d2e"),
  userText: chalk.hex("#c4a7e7"),
  assistantText: chalk.hex("#9ccfd8"),
  headingText: chalk.hex("#e0def4").bold,
  toolName: chalk.hex("#ebbcba"),
  toolLabel: chalk.hex("#6e6a86"),
  toolOutput: chalk.hex("#524f67"),
  toolRunning: chalk.hex("#ebbcba"),
  toolDone: chalk.hex("#31748f"),
  toolError: chalk.hex("#eb6f92"),
};

export const markdownTheme: MarkdownTheme = {
  heading: colors.headingText,
  link: chalk.hex("#c4a7e7").underline,
  linkUrl: colors.dim,
  code: chalk.hex("#ebbcba"),
  codeBlock: chalk.hex("#31748f"),
  codeBlockBorder: colors.border,
  quote: colors.dim.italic,
  quoteBorder: colors.border,
  hr: colors.border,
  listBullet: colors.rose,
  bold: chalk.bold,
  italic: chalk.italic,
  strikethrough: chalk.strikethrough,
  underline: chalk.underline,
};

export const defaultTextStyle: DefaultTextStyle = {
  color: colors.assistantText,
};

export const thinkingTextStyle: DefaultTextStyle = {
  color: colors.dim,
};

export const selectListTheme: SelectListTheme = {
  selectedPrefix: colors.accent,
  selectedText: colors.accentBold,
  description: colors.dim,
  scrollInfo: colors.dim,
  noMatch: colors.dim.italic,
};

export const editorTheme: EditorTheme = {
  borderColor: colors.border,
  selectList: selectListTheme,
};

export function applyTheme(overrides: ThemeOverrides = {}): void {
  Object.assign(themeState, overrides);

  colors.accent = chalk.hex(themeState.accent);
  colors.accentBold = chalk.hex(themeState.accent).bold;
  colors.border = chalk.hex(themeState.border);
  colors.userBackground = chalk.bgHex(themeState.userBackground);
  colors.userText = chalk.hex(themeState.userText);
  colors.assistantText = chalk.hex(themeState.assistantText);
  colors.headingText = chalk.hex(themeState.headingText).bold;
  colors.dim = chalk.hex(themeState.thinkingText);
  colors.dimBold = chalk.hex(themeState.thinkingText).bold;
  colors.toolName = chalk.hex(themeState.toolName);
  colors.toolOutput = chalk.hex(themeState.toolOutput);

  markdownTheme.heading = colors.headingText;
  markdownTheme.linkUrl = colors.dim;
  markdownTheme.code = chalk.hex(themeState.code);
  markdownTheme.codeBlock = chalk.hex(themeState.codeBlock);
  markdownTheme.codeBlockBorder = colors.border;
  markdownTheme.quote = colors.dim.italic;
  markdownTheme.quoteBorder = colors.border;
  markdownTheme.hr = colors.border;

  defaultTextStyle.color = colors.assistantText;
  thinkingTextStyle.color = colors.dim;

  selectListTheme.selectedPrefix = colors.accent;
  selectListTheme.selectedText = colors.accentBold;
  selectListTheme.description = colors.dim;
  selectListTheme.scrollInfo = colors.dim;
  selectListTheme.noMatch = colors.dim.italic;

  editorTheme.borderColor = colors.border;
}
