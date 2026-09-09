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


def test_upgrade_command_uv_and_pip():
    assert upgrade_command(
        _info(manager="uv-pip"), executable="/opt/venv/bin/python", which=_which_uv
    ) == ["uv", "pip", "install", "--python", "/opt/venv/bin/python", "--upgrade", "oddish"]
    assert upgrade_command(
        _info(manager="pip"), executable="/opt/venv/bin/python", which=lambda _n: None
    ) == ["/opt/venv/bin/python", "-m", "pip", "install", "--upgrade", "oddish"]


def test_upgrade_command_refusals():
    with pytest.raises(PackageError, match="editable"):
        upgrade_command(_info(source="editable", editable_path="/tmp/oddish"), which=_which_uv)
    with pytest.raises(PackageError, match="uv pip install oddish"):
        upgrade_command(_info(source="other"), which=_which_uv)
    with pytest.raises(PackageError, match="uv"):
        upgrade_command(_info(manager="uv-pip"), which=lambda _n: None)


def test_upgrade_command_force_reinstalls():
    uv_command = upgrade_command(
        _info(manager="uv-pip"),
        executable="/opt/venv/bin/python",
        which=_which_uv,
        force=True,
        pin_version="0.1.13",
    )
    assert uv_command == [
        "uv",
        "pip",
        "install",
        "--python",
        "/opt/venv/bin/python",
        "--reinstall-package",
        "oddish",
        "--upgrade",
        "oddish==0.1.13",
    ]
    pip_command = upgrade_command(
        _info(manager="pip"), executable="/opt/venv/bin/python", which=lambda _n: None, force=True
    )
    assert "--force-reinstall" in pip_command


def _patch_update(monkeypatch, info: InstallInfo, latest: str = "0.1.14") -> None:
    monkeypatch.setattr("oddish.cli.update.inspect_install", lambda: info)
    monkeypatch.setattr("oddish.cli.update.fetch_pypi_latest", lambda: latest)
    monkeypatch.setattr(
        "oddish.cli.update.upgrade_command",
        lambda _info, **_kwargs: ["echo", "uv pip install --upgrade oddish"],
    )


def test_update_dry_run_and_already_latest(monkeypatch):
    _patch_update(monkeypatch, _info())
    dry = runner.invoke(app, ["update", "--dry-run"])
    assert dry.exit_code == 0 and "uv pip install" in dry.stdout
    _patch_update(monkeypatch, _info(), latest="0.1.13")
    latest = runner.invoke(app, ["update"])
    assert latest.exit_code == 0 and "already up to date" in latest.stdout


def test_update_force_pins_when_current(monkeypatch):
    captured: list[dict] = []

    def _capture(_info, **kwargs):
        captured.append(kwargs)
        return ["uv", "pip", "install", "--reinstall-package", "oddish", "--upgrade", "oddish==0.1.13"]

    monkeypatch.setattr("oddish.cli.update.inspect_install", lambda: _info())
    monkeypatch.setattr("oddish.cli.update.fetch_pypi_latest", lambda: "0.1.13")
    monkeypatch.setattr("oddish.cli.update.upgrade_command", _capture)
    result = runner.invoke(app, ["update", "--force", "--dry-run"])
    assert result.exit_code == 0
    assert captured == [{"force": True, "pin_version": "0.1.13"}]
    monkeypatch.setattr("oddish.cli.update.fetch_pypi_latest", lambda: "0.2.0")
    captured.clear()
    result = runner.invoke(app, ["update", "--force", "--dry-run"])
    assert result.exit_code == 0
    assert captured == [{"force": True, "pin_version": None}]


def test_update_check_and_editable(monkeypatch):
    _patch_update(monkeypatch, _info(), latest="0.2.0")
    check = runner.invoke(app, ["update", "--check", "--json"])
    assert check.exit_code == 1 and '"update_available": true' in check.stdout
    monkeypatch.setattr(
        "oddish.cli.update.inspect_install",
        lambda: _info(source="editable", editable_path="/tmp/oddish"),
    )
    monkeypatch.setattr("oddish.cli.update.upgrade_command", upgrade_command)
    refused = runner.invoke(app, ["update"])
    assert refused.exit_code == 1 and "editable" in refused.output.lower()


def test_update_runs_and_names_pip(monkeypatch):
    _patch_update(monkeypatch, _info(manager="pip"))
    monkeypatch.setattr(
        "oddish.cli.update.upgrade_command", lambda _info, **_kwargs: ["echo", "upgrade"]
    )

    class _Result:
        returncode = 0

    monkeypatch.setattr(
        "oddish.cli.update.subprocess.run", lambda command, check=False, **_k: _Result()
    )
    monkeypatch.setattr("oddish.cli.update.query_installed_version", lambda _exe: "0.1.14")
    result = runner.invoke(app, ["update"])
    assert result.exit_code == 0
    assert "via pip" in result.output and "via uv pip" not in result.output
    assert "0.1.14" in result.stdout


def test_update_help_lists_options():
    result = runner.invoke(app, ["update", "--help"])
    assert result.exit_code == 0
    assert all(flag in result.output for flag in ("--dry-run", "--check", "--force"))
