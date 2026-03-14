import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.cli.commands import _load_runtime_config
from nanobot.config.schema import Config
from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.providers.openai_codex_provider import _strip_model_prefix
from nanobot.providers.registry import find_by_model
from nanobot.session.manager import SessionManager

runner = CliRunner()


@pytest.fixture
def mock_paths():
    """Mock config/workspace paths for test isolation."""
    with (
        patch("nanobot.config.loader.get_config_path") as mock_cp,
        patch("nanobot.config.loader.save_config") as mock_sc,
        patch("nanobot.config.loader.load_config") as mock_lc,
        patch("nanobot.utils.helpers.get_workspace_path") as mock_ws,
    ):
        base_dir = Path("./test_onboard_data")
        if base_dir.exists():
            shutil.rmtree(base_dir)
        base_dir.mkdir()

        config_file = base_dir / "config.json"
        workspace_dir = base_dir / "workspace"

        mock_cp.return_value = config_file
        mock_ws.return_value = workspace_dir
        mock_sc.side_effect = lambda config: config_file.write_text("{}")

        yield config_file, workspace_dir

        if base_dir.exists():
            shutil.rmtree(base_dir)


def test_onboard_fresh_install(mock_paths):
    """No existing config — should create from scratch."""
    config_file, workspace_dir = mock_paths

    result = runner.invoke(app, ["onboard"])

    assert result.exit_code == 0
    assert "Created config" in result.stdout
    assert "Created workspace" in result.stdout
    assert "nanobot is ready" in result.stdout
    assert config_file.exists()
    assert (workspace_dir / "AGENTS.md").exists()
    assert (workspace_dir / "memory" / "MEMORY.md").exists()


def test_onboard_existing_config_refresh(mock_paths):
    """Config exists, user declines overwrite — should refresh (load-merge-save)."""
    config_file, workspace_dir = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "existing values preserved" in result.stdout
    assert workspace_dir.exists()
    assert (workspace_dir / "AGENTS.md").exists()


def test_onboard_existing_config_overwrite(mock_paths):
    """Config exists, user confirms overwrite — should reset to defaults."""
    config_file, workspace_dir = mock_paths
    config_file.write_text('{"existing": true}')

    result = runner.invoke(app, ["onboard"], input="y\n")

    assert result.exit_code == 0
    assert "Config already exists" in result.stdout
    assert "Config reset to defaults" in result.stdout
    assert workspace_dir.exists()


def test_onboard_existing_workspace_safe_create(mock_paths):
    """Workspace exists — should not recreate, but still add missing templates."""
    config_file, workspace_dir = mock_paths
    workspace_dir.mkdir(parents=True)
    config_file.write_text("{}")

    result = runner.invoke(app, ["onboard"], input="n\n")

    assert result.exit_code == 0
    assert "Created workspace" not in result.stdout
    assert "Created AGENTS.md" in result.stdout
    assert (workspace_dir / "AGENTS.md").exists()


def test_config_matches_github_copilot_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "github-copilot/gpt-5.3-codex"

    assert config.get_provider_name() == "github_copilot"


def test_config_matches_openai_codex_with_hyphen_prefix():
    config = Config()
    config.agents.defaults.model = "openai-codex/gpt-5.1-codex"

    assert config.get_provider_name() == "openai_codex"


def test_find_by_model_prefers_explicit_prefix_over_generic_codex_keyword():
    spec = find_by_model("github-copilot/gpt-5.3-codex")

    assert spec is not None
    assert spec.name == "github_copilot"


def test_litellm_provider_canonicalizes_github_copilot_hyphen_prefix():
    provider = LiteLLMProvider(default_model="github-copilot/gpt-5.3-codex")

    resolved = provider._resolve_model("github-copilot/gpt-5.3-codex")

    assert resolved == "github_copilot/gpt-5.3-codex"


def test_openai_codex_strip_prefix_supports_hyphen_and_underscore():
    assert _strip_model_prefix("openai-codex/gpt-5.1-codex") == "gpt-5.1-codex"
    assert _strip_model_prefix("openai_codex/gpt-5.1-codex") == "gpt-5.1-codex"


def test_context_command_shows_session_breakdown(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "AGENTS.md").write_text("Project agent rules.", encoding="utf-8")

    config = Config()
    config.agents.defaults.workspace = str(workspace)

    sessions = SessionManager(workspace)
    s = sessions.get_or_create("cli:direct")
    s.add_message("user", "hello")
    s.add_message("assistant", "hi")
    sessions.save(s)

    with patch("nanobot.config.loader.load_config", return_value=config):
        result = runner.invoke(app, ["context", "--session", "cli:direct"])

    assert result.exit_code == 0
    assert "Context Usage By Session" in result.stdout
    assert "User Msgs" in result.stdout
    assert "System Prompt Breakdown" in result.stdout
    assert "Skills Breakdown" in result.stdout
    assert "Bootstrap: AGENTS.md" in result.stdout
    assert "No context usage data recorded yet" not in result.stdout


def test_context_command_no_usage_data(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    config = Config()
    config.agents.defaults.workspace = str(workspace)

    sessions = SessionManager(workspace)
    s = sessions.get_or_create("cli:direct")
    sessions.save(s)

    with patch("nanobot.config.loader.load_config", return_value=config):
        result = runner.invoke(app, ["context"])

    assert result.exit_code == 0
    assert "No context usage data recorded yet" in result.stdout


def test_context_command_recomputes_from_session_history(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    config = Config()
    config.agents.defaults.workspace = str(workspace)

    sessions = SessionManager(workspace)
    s = sessions.get_or_create("cli:direct")
    s.add_message("user", "Hello")
    s.add_message("assistant", "Hi there")
    s.add_message("user", "Can you summarize this session?")
    sessions.save(s)

    with patch("nanobot.config.loader.load_config", return_value=config):
        result = runner.invoke(app, ["context", "--session", "cli:direct"])

    assert result.exit_code == 0
    assert "Context Usage By Session" in result.stdout
    assert "User Msgs" in result.stdout

    reloaded = SessionManager(workspace).get_or_create("cli:direct")
    usage = reloaded.metadata.get("context_usage")
    assert isinstance(usage, dict)
    totals = usage.get("totals", {})
    assert isinstance(totals, dict)
    assert totals.get("user_messages") == 2
    assert int(totals.get("conversation_tokens", 0)) == int(
        totals.get("user_request_tokens", 0)
    ) + int(totals.get("agent_response_tokens", 0))


def test_context_command_prefers_persisted_assistant_usage(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)

    config = Config()
    config.agents.defaults.workspace = str(workspace)

    sessions = SessionManager(workspace)
    s = sessions.get_or_create("cli:direct")
    s.add_message("user", "hello")
    s.add_message(
        "assistant",
        "hi",
        usage={"prompt_tokens": 1234, "completion_tokens": 777},
    )
    sessions.save(s)

    with patch("nanobot.config.loader.load_config", return_value=config):
        result = runner.invoke(app, ["context", "--session", "cli:direct"])

    assert result.exit_code == 0

    reloaded = SessionManager(workspace).get_or_create("cli:direct")
    usage = reloaded.metadata.get("context_usage")
    assert isinstance(usage, dict)
    totals = usage.get("totals", {})
    assert isinstance(totals, dict)
    assert int(totals.get("agent_response_tokens", 0)) == 777
    assert int(totals.get("llm_prompt_tokens", 0)) == 1234
    assert int(totals.get("llm_completion_tokens", 0)) == 777
    assert int(totals.get("llm_total_tokens", 0)) == 2011


def test_agent_help_lists_config_and_workspace_flags():
    result = runner.invoke(app, ["agent", "--help"])

    assert result.exit_code == 0
    assert "--config" in result.stdout
    assert "--workspace" in result.stdout


def test_load_runtime_config_overrides_workspace(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "alt-workspace"

    config = _load_runtime_config(config_path, str(workspace))

    assert config.agents.defaults.workspace == str(workspace)
