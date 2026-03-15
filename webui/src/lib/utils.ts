import { Marked } from 'marked';
import type { TokenInfo } from '$lib/types';

const marked = new Marked({
  breaks: true,
  gfm: true
});

export function renderMarkdown(text: string): string {
  if (!text) return '';
  try {
    return marked.parse(text) as string;
  } catch {
    return escapeHtml(text);
  }
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

export function formatTimestamp(timestamp?: number): string {
  if (!timestamp) return '';

  return new Intl.DateTimeFormat(undefined, {
    hour: 'numeric',
    minute: '2-digit',
    month: 'short',
    day: 'numeric'
  }).format(new Date(timestamp));
}

export function relativeTimestamp(timestamp?: number): string {
  if (!timestamp) return 'No activity';

  const deltaMs = Date.now() - timestamp;
  const deltaMinutes = Math.max(0, Math.round(deltaMs / 60000));

  if (deltaMinutes < 1) return 'Just now';
  if (deltaMinutes < 60) return `${deltaMinutes}m ago`;

  const deltaHours = Math.round(deltaMinutes / 60);
  if (deltaHours < 24) return `${deltaHours}h ago`;

  const deltaDays = Math.round(deltaHours / 24);
  return `${deltaDays}d ago`;
}

export function clampText(value: string, max = 180): string {
  if (value.length <= max) return value;
  return `${value.slice(0, max - 1)}...`;
}

export function formatTokens(tokens: TokenInfo): string {
  const total = tokens.input + tokens.output + (tokens.reasoning || 0);
  if (total === 0) return '';
  if (total < 1000) return `${total} tok`;
  return `${(total / 1000).toFixed(1)}k tok`;
}

export function formatCost(cost: number): string {
  if (!cost || cost === 0) return '';
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(3)}`;
}
