"""Context references: expand @-references in user messages."""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.security.redact import redact_secrets

_REF_RE = re.compile(r"@(file|folder|url|diff|staged|git)(?::(\S+))?")

_DENY_PATTERNS = {".ssh/", ".gnupg/", ".env", "credentials", ".pem", "_rsa", "_key"}


def _is_denied(path_str: str) -> bool:
    """Check if a path matches the deny list."""
    lower = path_str.lower()
    return any(p in lower for p in _DENY_PATTERNS)


def _has_traversal(path_str: str, workspace: Path) -> bool:
    """Check for path traversal outside workspace."""
    try:
        resolved = (workspace / path_str).resolve()
        resolved.relative_to(workspace.resolve())
        return False
    except ValueError:
        return True


def _wrap_ref(tag: str, content: str) -> str:
    return f"[ref: {tag}]\n{content}\n[/ref]"


def _read_file(path_str: str, workspace: Path) -> str:
    """Read a file relative to workspace."""
    if _is_denied(path_str) or _has_traversal(path_str, workspace):
        return f"[blocked: {path_str}]"

    target = (workspace / path_str).resolve()
    if not target.is_file():
        return f"[not found: {path_str}]"

    try:
        content = target.read_text(encoding="utf-8")
        return redact_secrets(content)
    except Exception as e:
        return f"[error reading {path_str}: {e}]"


def _list_folder(path_str: str, workspace: Path) -> str:
    """List directory contents (depth 1)."""
    if _is_denied(path_str) or _has_traversal(path_str, workspace):
        return f"[blocked: {path_str}]"

    target = (workspace / path_str).resolve()
    if not target.is_dir():
        return f"[not a directory: {path_str}]"

    try:
        items = sorted(target.iterdir())
        lines = []
        for item in items[:200]:  # cap at 200 entries
            prefix = "📁 " if item.is_dir() else "📄 "
            lines.append(f"{prefix}{item.name}")
        return "\n".join(lines) if lines else "(empty directory)"
    except Exception as e:
        return f"[error listing {path_str}: {e}]"


def _fetch_url(url: str) -> str:
    """Fetch URL content synchronously."""
    import httpx

    if not url.startswith(("http://", "https://")):
        return "[blocked: only http/https URLs allowed]"

    try:
        with httpx.Client(timeout=10.0, follow_redirects=True, max_redirects=5) as client:
            r = client.get(url, headers={"User-Agent": "nanobot/1.0"})
            r.raise_for_status()
            content = r.text[:102400]  # 100KB max
            return redact_secrets(content)
    except Exception as e:
        return f"[error fetching {url}: {e}]"


def _git_command(args: list[str], workspace: Path) -> str:
    """Run a git command in the workspace."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stdout.strip() or result.stderr.strip()
        return redact_secrets(output) if output else "(no output)"
    except subprocess.TimeoutExpired:
        return "[git command timed out]"
    except Exception as e:
        return f"[git error: {e}]"


def expand_references(
    text: str,
    workspace: Path,
    context_tokens: int = 200_000,
) -> str:
    """Parse @-references in text, expand inline, return modified text.

    Token budget: expanded content limited to 50% of context_tokens (estimated as len//4).
    """
    max_chars = (context_tokens // 2) * 4  # 50% budget, ~4 chars per token
    total_chars = 0

    def _replace(match: re.Match) -> str:
        nonlocal total_chars
        ref_type = match.group(1)
        arg = match.group(2) or ""
        tag = match.group(0)

        if total_chars >= max_chars:
            return f"[ref: {tag} — budget exceeded]"

        if ref_type == "file":
            if not arg:
                return tag
            content = _read_file(arg, workspace)
        elif ref_type == "folder":
            if not arg:
                return tag
            content = _list_folder(arg, workspace)
        elif ref_type == "url":
            if not arg:
                return tag
            content = _fetch_url(arg)
        elif ref_type == "diff":
            content = _git_command(["diff"], workspace)
        elif ref_type == "staged":
            content = _git_command(["diff", "--staged"], workspace)
        elif ref_type == "git":
            n = 10
            if arg:
                try:
                    n = min(max(int(arg), 1), 50)
                except ValueError:
                    n = 10
            content = _git_command(["log", f"-{n}", "--oneline"], workspace)
        else:
            return tag

        # Enforce budget
        remaining = max_chars - total_chars
        if len(content) > remaining:
            content = content[:remaining] + "\n[truncated — budget limit]"
        total_chars += len(content)

        return _wrap_ref(tag, content)

    return _REF_RE.sub(_replace, text)
