"""Secret redaction pipeline — scans text for credentials and replaces them."""

import re
from typing import NamedTuple


class _Pattern(NamedTuple):
    label: str
    regex: re.Pattern[str]


_PATTERNS: list[_Pattern] = [
    # Private key blocks (must come before shorter patterns to avoid partial matches)
    _Pattern(
        "PRIVATE_KEY",
        re.compile(
            r"-----BEGIN\s+(?:RSA|EC|OPENSSH|PGP|DSA)?\s*PRIVATE KEY-----[\s\S]*?-----END",
            re.MULTILINE,
        ),
    ),
    # API keys
    _Pattern("OPENAI_KEY", re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    _Pattern("GITHUB_TOKEN", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    _Pattern("GOOGLE_KEY", re.compile(r"AIza[a-zA-Z0-9_-]{35}")),
    _Pattern("GROQ_KEY", re.compile(r"gsk_[a-zA-Z0-9]{20,}")),
    _Pattern("XAI_KEY", re.compile(r"xai-[a-zA-Z0-9]{20,}")),
    _Pattern("AWS_KEY", re.compile(r"AKIA[A-Z0-9]{16}")),
    _Pattern("GITLAB_TOKEN", re.compile(r"glpat-[a-zA-Z0-9_-]{20,}")),
    _Pattern("NPM_TOKEN", re.compile(r"npm_[a-zA-Z0-9]{36}")),
    _Pattern("PYPI_TOKEN", re.compile(r"pypi-[a-zA-Z0-9]{50,}")),
    # Telegram bot tokens
    _Pattern("TELEGRAM_TOKEN", re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")),
    # DB connection strings
    _Pattern(
        "DB_URI",
        re.compile(r"(?:postgres|mysql|mongodb(?:\+srv)?|redis)://[^\s\"']+:[^\s\"']+@"),
    ),
    # Auth headers
    _Pattern(
        "AUTH_HEADER",
        re.compile(
            r"(?:Authorization|X-Api-Key):\s*(?:Bearer|Basic|Token)\s+\S+", re.IGNORECASE
        ),
    ),
    # JSON credential fields
    _Pattern(
        "JSON_CREDENTIAL",
        re.compile(
            r'"(?:password|api_key|secret|token|credential|auth)"\s*:\s*"[^"]+"',
            re.IGNORECASE,
        ),
    ),
    # ENV assignments
    _Pattern(
        "ENV_SECRET",
        re.compile(
            r"(?:export\s+)?\w*(?:SECRET|KEY|TOKEN|PASSWORD|CREDENTIAL)\w*\s*=\s*\S+",
            re.IGNORECASE,
        ),
    ),
]


def redact_secrets(text: str) -> str:
    """Apply all redaction patterns, return sanitized text."""
    for pat in _PATTERNS:
        text = pat.regex.sub(f"[REDACTED:{pat.label}]", text)
    return text


def has_secrets(text: str) -> bool:
    """Fast check without replacement."""
    return any(pat.regex.search(text) for pat in _PATTERNS)
