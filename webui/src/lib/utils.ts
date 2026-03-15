import { Marked } from 'marked';
import type { TokenInfo } from '$lib/types';

const marked = new Marked({
  breaks: true,
  gfm: true
});

// Tags allowed in rendered markdown output. Everything else is stripped.
const ALLOWED_TAGS = new Set([
  'p', 'br', 'strong', 'b', 'em', 'i', 'u', 's', 'del',
  'code', 'pre', 'blockquote',
  'ul', 'ol', 'li',
  'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'a', 'img',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
  'hr', 'sup', 'sub', 'details', 'summary',
]);

const ALLOWED_ATTRS: Record<string, Set<string>> = {
  a: new Set(['href', 'title']),
  img: new Set(['src', 'alt', 'title']),
  td: new Set(['align']),
  th: new Set(['align']),
};

function sanitizeHtml(html: string): string {
  // Strip dangerous tags and attributes while keeping allowed markdown output
  return html.replace(/<\/?([a-zA-Z][a-zA-Z0-9]*)\b([^>]*)?\/?>/g, (match, tag, attrs) => {
    const lowerTag = tag.toLowerCase();
    if (!ALLOWED_TAGS.has(lowerTag)) {
      return '';
    }
    // Closing tag
    if (match.startsWith('</')) {
      return `</${lowerTag}>`;
    }
    // Filter attributes
    const allowedAttrs = ALLOWED_ATTRS[lowerTag];
    if (!allowedAttrs || !attrs) {
      const selfClose = match.endsWith('/>') ? ' /' : '';
      return `<${lowerTag}${selfClose}>`;
    }
    const cleanAttrs = (attrs as string)
      .match(/\s+([a-zA-Z-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)
      ?.filter((attr: string) => {
        const name = attr.trim().split(/\s*=/)[0].toLowerCase();
        return allowedAttrs.has(name);
      })
      ?.map((attr: string) => {
        // Block javascript: URLs
        if (/javascript\s*:/i.test(attr)) return '';
        return attr;
      })
      .join('') ?? '';
    const selfClose = match.endsWith('/>') ? ' /' : '';
    return `<${lowerTag}${cleanAttrs}${selfClose}>`;
  });
}

export function renderMarkdown(text: string): string {
  if (!text) return '';
  try {
    const raw = marked.parse(text) as string;
    return sanitizeHtml(raw);
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
