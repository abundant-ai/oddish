"""``oddish version``, ``oddish --version``, and install inspection."""

from __future__ import annotations

import json

from oddish.cli import app
from oddish.cli._package import InstallInfo, inspect_install, is_outdated
from typer.testing import CliRunner

runner = CliRunner()


class _FakeDist:
    def __init__(self, installer: str | None, direct_url: dict | None) -> None:
        self._installer = installer
        self._direct_url = direct_url

    def read_text(self, name: str) -> str | None:
        if name == "INSTALLER":
            return self._installer
        if name == "direct_url.json" and self._direct_url is not None:
            return json.dumps(self._direct_url)
        return None


def _pypi_info(**overrides) -> InstallInfo:
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


def test_inspect_install_pypi(monkeypatch):
    monkeypatch.setattr("oddish.cli._package.installed_version", lambda: "0.1.13")
    info = inspect_install(which=_which_uv, distribution=_FakeDist("uv", None))
    assert info.source == "pypi"
    assert info.version == "0.1.13"
    assert info.manager == "uv-pip"


def test_inspect_install_pypi_wheel_url_stays_pypi(monkeypatch):
    monkeypatch.setattr("oddish.cli._package.installed_version", lambda: "0.1.13")
    info = inspect_install(
        which=_which_uv,
        distribution=_FakeDist(
            "uv",
            {
                "url": "https://files.pythonhosted.org/packages/od/oddish-0.1.13-py3-none-any.whl",
                "archive_info": {"hash": "sha256=abc"},
            },
        ),
    )
    assert info.source == "pypi"


def test_inspect_install_git_is_other(monkeypatch):
    monkeypatch.setattr("oddish.cli._package.installed_version", lambda: "0.1.13")
    info = inspect_install(
        which=_which_uv,
        distribution=_FakeDist(
            "uv",
            {
                "url": "https://github.com/abundant-ai/oddish.git",
                "vcs_info": {"vcs": "git", "requested_revision": "main"},
                "subdirectory": "oddish",
            },
        ),
    )
    assert info.source == "other"


def test_inspect_install_ignores_malformed_direct_url(monkeypatch):
    monkeypatch.setattr("oddish.cli._package.installed_version", lambda: "0.1.13")

    class _Broken:
        def read_text(self, name: str) -> str | None:
            if name == "direct_url.json":
                return "{not-json"
            return "uv"

    info = inspect_install(which=_which_uv, distribution=_Broken())  # type: ignore[arg-type]
    assert info.source == "pypi"


def test_is_outdated():
    assert is_outdated("0.1.13", "0.1.14")
    assert not is_outdated("0.1.13", "0.1.13")
    assert not is_outdated("0.1.14", "0.1.13")


def test_version_flag_prints_package_version(monkeypatch):
    monkeypatch.setattr("oddish.cli.version.inspect_install", lambda: _pypi_info())
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "oddish 0.1.13"


def test_version_command_includes_source(monkeypatch):
    monkeypatch.setattr("oddish.cli.version.inspect_install", lambda: _pypi_info())
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0, result.output
    assert "oddish 0.1.13" in result.stdout
    assert "PyPI" in result.stdout
    assert "uv pip" in result.stdout


def test_version_json(monkeypatch):
    monkeypatch.setattr("oddish.cli.version.inspect_install", lambda: _pypi_info())
    result = runner.invoke(app, ["version", "--json"])
    assert result.exit_code == 0, result.output
    assert '"version": "0.1.13"' in result.stdout
    assert '"source": "pypi"' in result.stdout
    assert "latest" not in result.stdout


def test_version_check_outdated_exits_one(monkeypatch):
    monkeypatch.setattr("oddish.cli.version.inspect_install", lambda: _pypi_info())
    monkeypatch.setattr("oddish.cli.version.fetch_pypi_latest", lambda: "0.1.14")
    result = runner.invoke(app, ["version", "--check"])
    assert result.exit_code == 1, result.output
    assert "0.1.14" in result.stdout
    assert "update available" in result.stdout


def test_version_check_current_exits_zero(monkeypatch):
    monkeypatch.setattr("oddish.cli.version.inspect_install", lambda: _pypi_info())
    monkeypatch.setattr("oddish.cli.version.fetch_pypi_latest", lambda: "0.1.13")
    result = runner.invoke(app, ["version", "--check", "--json"])
    assert result.exit_code == 0, result.output
    assert '"update_available": false' in result.stdout
    assert '"latest": "0.1.13"' in result.stdout
