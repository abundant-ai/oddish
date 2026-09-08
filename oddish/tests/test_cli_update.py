"""``oddish update`` for ``uv pip install oddish``."""

from __future__ import annotations

import pytest
from oddish.cli import app
from oddish.cli._package import InstallInfo, PackageError, upgrade_command
from typer.testing import CliRunner

runner = CliRunner()


def _info(**overrides) -> InstallInfo:
    payload = {
        "version": "0.1.13",
        "source": "pypi",
        "manager": "uv-pip",
        "installer": "uv",
    }
    payload.update(overrides)
    return InstallInfo(**payload)


def _which_uv(name: str) -> str | None:
    return "/usr/bin/uv" if name == "uv" else None


def test_upgrade_command_uv_pip_pypi():
    command = upgrade_command(
        _info(manager="uv-pip"),
        executable="/opt/venv/bin/python",
        which=_which_uv,
    )
    assert command == [
        "uv",
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "--upgrade",
        "oddish",
    ]


def test_upgrade_command_pip_fallback():
    command = upgrade_command(
        _info(manager="pip"),
        executable="/opt/venv/bin/python",
        which=lambda _name: None,
    )
    assert command == [
        "/opt/venv/bin/python",
        "-m",
        "pip",
        "install",
        "--upgrade",
        "oddish",
    ]


def test_upgrade_command_refuses_editable():
    with pytest.raises(PackageError, match="editable"):
        upgrade_command(
            _info(source="editable", editable_path="/Users/me/oddish/oddish"),
            which=_which_uv,
        )


def test_upgrade_command_refuses_non_pypi():
    with pytest.raises(PackageError, match="uv pip install oddish"):
        upgrade_command(_info(source="other"), which=_which_uv)


def test_upgrade_command_missing_uv():
    with pytest.raises(PackageError, match="uv"):
        upgrade_command(_info(manager="uv-pip"), which=lambda _name: None)


def _patch_update(monkeypatch, info: InstallInfo, latest: str = "0.1.14") -> None:
    monkeypatch.setattr("oddish.cli.update.inspect_install", lambda: info)
    monkeypatch.setattr("oddish.cli.update.fetch_pypi_latest", lambda: latest)
    monkeypatch.setattr(
        "oddish.cli.update.upgrade_command",
        lambda _info: ["uv", "pip", "install", "--upgrade", "oddish"],
    )


def test_update_dry_run(monkeypatch):
    _patch_update(monkeypatch, _info())
    result = runner.invoke(app, ["update", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "uv pip install" in result.stdout
    assert "oddish" in result.stdout


def test_update_already_latest(monkeypatch):
    _patch_update(monkeypatch, _info(), latest="0.1.13")
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert "already up to date" in result.stdout


def test_update_check_outdated(monkeypatch):
    _patch_update(monkeypatch, _info(), latest="0.2.0")
    result = runner.invoke(app, ["update", "--check", "--json"])
    assert result.exit_code == 1, result.output
    assert '"action": "check"' in result.stdout
    assert '"update_available": true' in result.stdout


def test_update_refuses_editable(monkeypatch):
    monkeypatch.setattr(
        "oddish.cli.update.inspect_install",
        lambda: _info(source="editable", editable_path="/tmp/oddish"),
    )
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 1, result.output
    assert "editable" in result.output.lower()


def test_update_progress_uses_pip_manager_label(monkeypatch):
    _patch_update(monkeypatch, _info(manager="pip"))
    monkeypatch.setattr(
        "oddish.cli.update.upgrade_command",
        lambda _info: ["echo", "upgrade"],
    )

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        "oddish.cli.update.subprocess.run",
        lambda command, check=False, **_kwargs: _Result(),
    )
    monkeypatch.setattr(
        "oddish.cli.update.query_installed_version",
        lambda _exe: "0.1.14",
    )
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert "via pip" in result.output
    assert "via uv pip" not in result.output


def test_update_runs_upgrade(monkeypatch):
    monkeypatch.setattr("oddish.cli.update.inspect_install", lambda: _info())
    monkeypatch.setattr("oddish.cli.update.fetch_pypi_latest", lambda: "0.1.14")
    monkeypatch.setattr(
        "oddish.cli.update.upgrade_command",
        lambda _info: ["echo", "upgrade"],
    )

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        "oddish.cli.update.subprocess.run",
        lambda command, check=False, **_kwargs: _Result(),
    )
    monkeypatch.setattr(
        "oddish.cli.update.query_installed_version",
        lambda _exe: "0.1.14",
    )
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert "0.1.14" in result.stdout


def test_update_help_lists_options():
    result = runner.invoke(app, ["update", "--help"])
    assert result.exit_code == 0, result.output
    assert "--dry-run" in result.output
    assert "--check" in result.output
    assert "uv pip install oddish" in result.output
