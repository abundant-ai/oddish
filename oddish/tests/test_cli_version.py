"""``oddish version``, ``oddish --version``, and install inspection."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from oddish.cli import app
from oddish.cli._package import (
    InstallInfo,
    PackageError,
    fetch_pypi_latest,
    inspect_install,
    is_outdated,
)

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
    payload = {"version": "0.1.13", "source": "pypi", "manager": "uv-pip", "installer": "uv"}
    payload.update(overrides)
    return InstallInfo(**payload)


def _which_uv(name: str) -> str | None:
    return "/usr/bin/uv" if name == "uv" else None


def test_inspect_install_sources(monkeypatch):
    monkeypatch.setattr("oddish.cli._package.installed_version", lambda: "0.1.13")
    pypi = inspect_install(which=_which_uv, distribution=_FakeDist("uv", None))
    assert pypi.source == "pypi" and pypi.manager == "uv-pip"
    wheel = inspect_install(
        which=_which_uv,
        distribution=_FakeDist(
            "uv",
            {"url": "https://files.pythonhosted.org/packages/od/oddish.whl", "archive_info": {}},
        ),
    )
    assert wheel.source == "pypi"
    git = inspect_install(
        which=_which_uv,
        distribution=_FakeDist(
            "uv",
            {"url": "https://github.com/abundant-ai/oddish.git", "vcs_info": {"vcs": "git"}},
        ),
    )
    local = inspect_install(
        which=_which_uv,
        distribution=_FakeDist("uv", {"url": "file:///tmp/oddish.whl", "archive_info": {}}),
    )
    assert git.source == "other" and local.source == "other"

    class _Broken:
        def read_text(self, name: str) -> str | None:
            return "{not-json" if name == "direct_url.json" else "uv"

    assert inspect_install(which=_which_uv, distribution=_Broken()).source == "pypi"  # type: ignore[arg-type]


def test_fetch_pypi_latest_rejects_non_json(monkeypatch):
    class _Resp:
        def json(self) -> object:
            raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr("oddish.cli._package.httpx.get", lambda *_a, **_k: _Resp())
    with pytest.raises(PackageError, match="PyPI"):
        fetch_pypi_latest()


def test_is_outdated():
    assert is_outdated("0.1.13", "0.1.14")
    assert not is_outdated("0.1.13", "0.1.13")
    assert not is_outdated("0.1.14", "0.1.13")


def test_version_command(monkeypatch):
    monkeypatch.setattr("oddish.cli.version.inspect_install", lambda: _pypi_info())
    flag = runner.invoke(app, ["--version"])
    assert flag.exit_code == 0 and flag.stdout.strip() == "oddish 0.1.13"
    text = runner.invoke(app, ["version"])
    assert text.exit_code == 0 and "PyPI" in text.stdout and "uv pip" in text.stdout
    data = runner.invoke(app, ["version", "--json"])
    assert data.exit_code == 0 and '"source": "pypi"' in data.stdout and "latest" not in data.stdout


def test_version_check(monkeypatch):
    monkeypatch.setattr("oddish.cli.version.inspect_install", lambda: _pypi_info())
    monkeypatch.setattr("oddish.cli.version.fetch_pypi_latest", lambda: "0.1.14")
    outdated = runner.invoke(app, ["version", "--check"])
    assert outdated.exit_code == 1 and "update available" in outdated.stdout
    monkeypatch.setattr("oddish.cli.version.fetch_pypi_latest", lambda: "0.1.13")
    current = runner.invoke(app, ["version", "--check", "--json"])
    assert current.exit_code == 0 and '"update_available": false' in current.stdout
    monkeypatch.setattr(
        "oddish.cli.version.inspect_install",
        lambda: _pypi_info(source="editable", editable_path="file:///tmp/oddish"),
    )
    monkeypatch.setattr("oddish.cli.version.fetch_pypi_latest", lambda: "0.1.14")
    other = runner.invoke(app, ["version", "--check"])
    assert other.exit_code == 0 and "not from PyPI" in other.stdout and "up to date" not in other.stdout
    assert '"update_available": null' in runner.invoke(app, ["version", "--check", "--json"]).stdout
    def _fail_pypi() -> str:
        raise PackageError("Could not reach PyPI")

    monkeypatch.setattr("oddish.cli.version.fetch_pypi_latest", _fail_pypi)
    failed = runner.invoke(app, ["version", "--check", "--json"])
    assert failed.exit_code == 1 and '"action": "error"' in failed.stdout
