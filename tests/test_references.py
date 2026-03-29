"""Tests for the context references module."""

import pytest
from pathlib import Path
from unittest.mock import patch

from nanobot.agent.references import expand_references


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with test files."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')\n")
    (tmp_path / "readme.md").write_text("# Project\nA test project.\n")
    (tmp_path / "empty_dir").mkdir()
    (tmp_path / ".env").write_text("SECRET=abc123\n")
    return tmp_path


def test_file_reference(workspace: Path) -> None:
    result = expand_references("Check @file:readme.md please", workspace)
    assert "[ref: @file:readme.md]" in result
    assert "# Project" in result
    assert "[/ref]" in result


def test_file_reference_subdir(workspace: Path) -> None:
    result = expand_references("See @file:src/main.py", workspace)
    assert "print('hello')" in result


def test_folder_reference(workspace: Path) -> None:
    result = expand_references("List @folder:src/", workspace)
    assert "[ref: @folder:src/]" in result
    assert "main.py" in result


def test_url_reference() -> None:
    with patch("nanobot.agent.references._fetch_url", return_value="fetched content"):
        result = expand_references("See @url:https://example.com", Path("/tmp"))
        assert "[ref: @url:https://example.com]" in result
        assert "fetched content" in result


def test_diff_reference(workspace: Path) -> None:
    with patch("nanobot.agent.references._git_command", return_value="diff output"):
        result = expand_references("Show @diff", workspace)
        assert "[ref: @diff]" in result
        assert "diff output" in result


def test_staged_reference(workspace: Path) -> None:
    with patch("nanobot.agent.references._git_command", return_value="staged output"):
        result = expand_references("Show @staged", workspace)
        assert "staged output" in result


def test_git_log_reference(workspace: Path) -> None:
    with patch("nanobot.agent.references._git_command", return_value="abc123 commit") as mock:
        result = expand_references("Show @git:5", workspace)
        assert "abc123 commit" in result
        mock.assert_called_with(["log", "-5", "--oneline"], workspace)


def test_git_log_default_count(workspace: Path) -> None:
    with patch("nanobot.agent.references._git_command", return_value="log") as mock:
        expand_references("Show @git", workspace)
        mock.assert_called_with(["log", "-10", "--oneline"], workspace)


def test_git_log_max_capped(workspace: Path) -> None:
    with patch("nanobot.agent.references._git_command", return_value="log") as mock:
        expand_references("Show @git:100", workspace)
        mock.assert_called_with(["log", "-50", "--oneline"], workspace)


def test_blocked_path_traversal(workspace: Path) -> None:
    result = expand_references("See @file:../../etc/passwd", workspace)
    assert "[blocked:" in result


def test_blocked_deny_list(workspace: Path) -> None:
    result = expand_references("See @file:.env", workspace)
    assert "[blocked:" in result


def test_blocked_ssh(workspace: Path) -> None:
    result = expand_references("See @file:.ssh/id_rsa", workspace)
    assert "[blocked:" in result


def test_blocked_credentials(workspace: Path) -> None:
    result = expand_references("See @file:credentials.json", workspace)
    assert "[blocked:" in result


def test_blocked_pem(workspace: Path) -> None:
    result = expand_references("See @file:server.pem", workspace)
    assert "[blocked:" in result


def test_blocked_url_scheme() -> None:
    with patch("nanobot.agent.references._fetch_url") as mock:
        mock.return_value = "[blocked: only http/https URLs allowed]"
        result = expand_references("See @url:ftp://example.com/file", Path("/tmp"))
        assert "[blocked:" in result


def test_token_budget_enforcement(workspace: Path) -> None:
    # Create a file larger than the budget allows (budget = 1000 tokens * 50% * 4 = 2000 chars)
    (workspace / "big.txt").write_text("hello world\n" * 500)
    result = expand_references("See @file:big.txt", workspace, context_tokens=1000)
    assert "[truncated — budget limit]" in result


def test_file_not_found(workspace: Path) -> None:
    result = expand_references("See @file:nonexistent.txt", workspace)
    assert "[not found:" in result


def test_no_references_passthrough() -> None:
    text = "Hello, no references here."
    assert expand_references(text, Path("/tmp")) == text


def test_redaction_applied(workspace: Path) -> None:
    """Secrets in expanded content should be redacted."""
    (workspace / "secrets.txt").write_text("key=sk-abc123def456ghi789jkl012mno\n")
    result = expand_references("See @file:secrets.txt", workspace)
    assert "[REDACTED:" in result
    assert "sk-abc123" not in result
