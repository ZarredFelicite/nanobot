import io
import shutil
from email.message import Message
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from urllib.error import HTTPError

import pytest
from typer.testing import CliRunner

from nanobot.cli.commands import app
from nanobot.cli.commands import _resolve_external_tui_binary
from nanobot.cli.commands import _load_runtime_config
from nanobot.config.schema import Config
from nanobot.providers.litellm_provider import LiteLLMProvider
from nanobot.providers.openai_codex_provider import _strip_model_prefix
from nanobot.providers.registry import find_by_model
from nanobot.session.manager import SessionManager
from nanobot.utils.model_probe import ModelProbeResult
from nanobot.utils.model_probe import collect_configured_models

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


def test_agent_launches_external_tui_when_no_message(tmp_path):
    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "workspace")
    config.channels.opencode.port = 4096

    with (
        patch("nanobot.cli.commands._load_runtime_config", return_value=config),
        patch("nanobot.cli.commands.sync_workspace_templates"),
        patch("nanobot.cli.commands._launch_external_tui") as mock_launch,
    ):
        result = runner.invoke(app, ["agent"])

    assert result.exit_code == 0
    mock_launch.assert_called_once_with(4096)


def test_resolve_external_tui_binary_prefers_repo_local_script():
    with (
        patch("pathlib.Path.is_file", lambda self: str(self).endswith("tui/nanobot-tui")),
        patch("nanobot.cli.commands.shutil.which", return_value="/usr/bin/nanobot-tui"),
    ):
        resolved = _resolve_external_tui_binary()

    assert resolved is not None
    assert resolved.endswith("tui/nanobot-tui")


def test_resolve_external_tui_binary_falls_back_to_path():
    with (
        patch("pathlib.Path.is_file", return_value=False),
        patch("nanobot.cli.commands.shutil.which", return_value="/usr/bin/nanobot-tui"),
    ):
        resolved = _resolve_external_tui_binary()

    assert resolved == "/usr/bin/nanobot-tui"


def test_load_runtime_config_overrides_workspace(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    workspace = tmp_path / "alt-workspace"

    config = _load_runtime_config(config_path, str(workspace))

    assert config.agents.defaults.workspace == str(workspace)


def test_reload_config_command_hits_gateway_endpoint():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok": true, "model": "openrouter/minimax/minimax-m2.5"}'

    with patch("urllib.request.urlopen", return_value=Response()) as mock_urlopen:
        result = runner.invoke(app, ["reload-config", "--host", "127.0.0.1", "--port", "18790"])

    assert result.exit_code == 0
    assert "Reloaded config." in result.stdout
    req = mock_urlopen.call_args.args[0]
    assert req.full_url == "http://127.0.0.1:18790/config/reload"
    assert req.get_method() == "POST"


def test_reload_config_command_handles_http_error():
    err = HTTPError(
        url="http://127.0.0.1:18790/config/reload",
        code=500,
        msg="boom",
        hdrs=Message(),
        fp=io.BytesIO(b'{"ok": false, "error": "boom"}'),
    )

    with patch("urllib.request.urlopen", side_effect=err):
        result = runner.invoke(app, ["reload-config"])

    assert result.exit_code == 1
    assert "Gateway rejected reload request" in result.stdout


def test_collect_configured_models_includes_decision_models_and_ignores_alias_targets():
    config = Config()
    config.agents.defaults.model = "openrouter/minimax/minimax-m2.5"
    config.models.primary = "openrouter/minimax/minimax-m2.5"
    config.models.fallbacks = [
        "openai-codex/gpt-5.3-codex",
        "anthropic/claude-sonnet-4-20250514",
    ]
    config.tools.subconscious.classifier_model = "openrouter/google/gemini-2.0-flash-lite-001"
    config.gateway.heartbeat.decide_model = "openai/gpt-5-mini"
    config.models.aliases = {
        "fast": "openrouter/minimax/minimax-m2.5",
        "smart": "anthropic/claude-sonnet-4-20250514",
    }

    models = collect_configured_models(config)

    assert [entry.model for entry in models] == [
        "openrouter/minimax/minimax-m2.5",
        "openai-codex/gpt-5.3-codex",
        "anthropic/claude-sonnet-4-20250514",
        "openrouter/google/gemini-2.0-flash-lite-001",
        "openai/gpt-5-mini",
    ]
    assert models[0].sources == ["default", "primary"]
    assert models[1].auth_mode == "oauth"
    assert models[3].sources == ["subconscious-decision"]
    assert models[4].sources == ["heartbeat-decision"]


def test_models_list_command_renders_configured_models():
    config = Config()
    config.agents.defaults.model = "openrouter/minimax/minimax-m2.5"
    config.models.fallbacks = ["openai-codex/gpt-5.3-codex"]
    config.models.aliases = {"codex": "openai-codex/gpt-5.3-codex"}

    with patch("nanobot.cli.commands._load_runtime_config", return_value=config):
        result = runner.invoke(app, ["models", "list"])

    assert result.exit_code == 0
    assert "Configured Models" in result.stdout
    assert "openrouter/minimax/mini" in result.stdout
    assert "openai-codex/gpt-5.3-co" in result.stdout
    assert "subconscious-decision" in result.stdout
    assert "Aliases" not in result.stdout


def test_probe_litellm_falls_back_when_stream_has_no_text():
    import asyncio

    from nanobot.utils import model_probe

    provider = Mock()
    provider._resolve_model.return_value = "openrouter/minimax/minimax-m2.5"
    provider._extra_msg_keys.return_value = frozenset()
    provider._sanitize_empty_content.side_effect = lambda messages: messages
    provider._sanitize_messages.side_effect = lambda messages, extra_keys=frozenset(): messages
    provider._apply_model_overrides.return_value = None
    provider.api_key = "key"
    provider.api_base = None
    provider.extra_headers = {}

    class EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    provider.chat = AsyncMock(return_value=type("Resp", (), {"content": "NANOBOT_MODEL_TEST_OK"})())

    with patch("litellm.acompletion", new=AsyncMock(return_value=EmptyStream())):
        result = asyncio.run(
            model_probe._probe_litellm(
                provider,
                model="openrouter/minimax/minimax-m2.5",
                exact_text="NANOBOT_MODEL_TEST_OK",
            )
        )

    assert result["ttft_s"] is None
    assert result["actual_text"] == "NANOBOT_MODEL_TEST_OK"
    provider.chat.assert_awaited_once()


def test_models_test_command_reports_results():
    config = Config()
    fake_results = [
        ModelProbeResult(
            model="openrouter/minimax/minimax-m2.5",
            provider_name="openrouter",
            ttft_s=0.42,
            total_s=1.23,
            expected_text="NANOBOT_MODEL_TEST_OK",
            actual_text="NANOBOT_MODEL_TEST_OK",
            exact_match=True,
        ),
        ModelProbeResult(
            model="openai-codex/gpt-5.3-codex",
            provider_name="openai_codex",
            expected_text="NANOBOT_MODEL_TEST_OK",
            actual_text="close but wrong",
            exact_match=False,
            error="close but wrong",
        ),
    ]

    with (
        patch("nanobot.cli.commands._load_runtime_config", return_value=config),
        patch("nanobot.cli.commands.collect_configured_models", return_value=[object(), object()]),
        patch(
            "nanobot.cli.commands.probe_configured_models", new=AsyncMock(return_value=fake_results)
        ),
    ):
        result = runner.invoke(app, ["models", "test"])

    assert result.exit_code == 1
    assert "Model Probe Results" in result.stdout
    assert "openrouter/minim" in result.stdout
    assert "0.42s" in result.stdout
    assert "close but wrong" in result.stdout
    assert "Passed:" in result.stdout
