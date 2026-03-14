from pathlib import Path

from nanobot.config import loader
from nanobot.utils.helpers import get_data_path, get_workspace_path


def test_data_dir_follows_active_config_path(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "instance-a" / "config.json"
    monkeypatch.setattr(loader, "_current_config_path", None)

    loader.set_config_path(config_path)

    assert loader.get_config_path() == config_path
    assert loader.get_data_dir() == config_path.parent
    assert get_data_path() == config_path.parent


def test_default_workspace_uses_active_data_dir(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "instance-b" / "config.json"
    monkeypatch.setattr(loader, "_current_config_path", None)

    loader.set_config_path(config_path)

    assert get_workspace_path() == config_path.parent / "workspace"


def test_gateway_help_lists_config_flag() -> None:
    from typer.testing import CliRunner

    from nanobot.cli.commands import app

    result = CliRunner().invoke(app, ["gateway", "--help"])

    assert result.exit_code == 0
    assert "--config" in result.stdout
