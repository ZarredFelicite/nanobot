"""Tests for the secret redaction pipeline."""

import pytest

from nanobot.security.redact import has_secrets, redact_secrets


@pytest.mark.parametrize(
    "text,label",
    [
        ("sk-abc123def456ghi789jkl012mno", "OPENAI_KEY"),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "GITHUB_TOKEN"),
        ("AIzaSyA-abcdefghijklmnopqrstuvwxyz12345", "GOOGLE_KEY"),
        ("gsk_abcdef1234567890abcdef", "GROQ_KEY"),
        ("xai-abcdef1234567890abcdef", "XAI_KEY"),
        ("AKIAIOSFODNN7EXAMPLE", "AWS_KEY"),
        ("glpat-abcdefghijklmnopqrstu", "GITLAB_TOKEN"),
        ("npm_abcdefghijklmnopqrstuvwxyz0123456789", "NPM_TOKEN"),
        ("pypi-" + "a" * 50, "PYPI_TOKEN"),
        ("1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ12345678_", "TELEGRAM_TOKEN"),
        ("postgres://user:pass@host:5432/db", "DB_URI"),
        ("mongodb+srv://admin:secret@cluster.example.com/mydb", "DB_URI"),
        ("redis://user:password@redis.example.com:6379", "DB_URI"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc", "AUTH_HEADER"),
        ("X-Api-Key: Token my-secret-token-value", "AUTH_HEADER"),
        ('"password": "super_secret_123"', "JSON_CREDENTIAL"),
        ('"api_key": "sk-something"', "JSON_CREDENTIAL"),
        ("export MY_SECRET_KEY=abc123", "ENV_SECRET"),
        ("DATABASE_PASSWORD=hunter2", "ENV_SECRET"),
        ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "ENV_SECRET"),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END",
            "PRIVATE_KEY",
        ),
        (
            "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Blbn...\n-----END",
            "PRIVATE_KEY",
        ),
    ],
)
def test_redact_pattern(text: str, label: str) -> None:
    """Each known pattern is detected and replaced."""
    result = redact_secrets(text)
    assert f"[REDACTED:{label}]" in result, f"Expected [REDACTED:{label}] in: {result}"
    assert has_secrets(text)


@pytest.mark.parametrize(
    "text",
    [
        "Hello, how are you?",
        "The file is at /home/user/docs/readme.md",
        "sk-short",  # too short to be a real key
        "Use export PATH=/usr/local/bin:$PATH",
        "1234:short",  # too few digits for telegram
        '"name": "not a secret"',
        "http://example.com/page",
        "MY_VARIABLE=hello",  # no secret/key/token/password keyword
    ],
)
def test_no_false_positives(text: str) -> None:
    """Common non-secret text should not trigger redaction."""
    assert redact_secrets(text) == text
    assert not has_secrets(text)


def test_multi_pattern_text() -> None:
    """Multiple secrets in one block are all redacted."""
    text = (
        'Config:\n'
        'export OPENAI_KEY=sk-abc123def456ghi789jkl012mno\n'
        '"password": "hunter2"\n'
        'Authorization: Bearer eyJtoken\n'
    )
    result = redact_secrets(text)
    assert "[REDACTED:ENV_SECRET]" in result
    assert "[REDACTED:JSON_CREDENTIAL]" in result
    assert "[REDACTED:AUTH_HEADER]" in result
    assert "hunter2" not in result
    assert "sk-abc123" not in result


def test_redact_preserves_surrounding_text() -> None:
    """Non-secret content around a secret is preserved."""
    text = "The key is ghp_abcdefghijklmnopqrstuvwxyz0123456789 and that's it."
    result = redact_secrets(text)
    assert result.startswith("The key is [REDACTED:")
    assert result.endswith("and that's it.")
