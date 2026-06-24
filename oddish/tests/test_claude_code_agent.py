"""OddishClaudeCode: harbor-pin requirement + in-sandbox install."""

from __future__ import annotations

import pytest

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE
from oddish.workers.agents import claude_code as claude_code_agent
from oddish.workers.agents.claude_code import (
    OddishClaudeCode,
    _pinned_harbor_requirement,
)


def test_requirement_explicit_source_sha_is_git_reference():
    req = _pinned_harbor_requirement("https://github.com/dot-agi/harbor", "a" * 40)
    assert req == f"harbor @ git+https://github.com/dot-agi/harbor@{'a' * 40}"


def test_requirement_from_installed_direct_url_is_git_reference():
    # The orchestrator's harbor is git-installed from the locked fork, so the
    # requirement derived from its direct_url is the locked default pin.
    req = _pinned_harbor_requirement()
    assert req == f"harbor @ git+{HARBOR_DEFAULT_SOURCE}@{HARBOR_DEFAULT_SHA}"


def test_requirement_falls_back_to_version_without_direct_url(monkeypatch):
    monkeypatch.setattr(claude_code_agent, "_installed_harbor_git_pin", lambda: None)
    monkeypatch.setattr(claude_code_agent, "version", lambda _name: "9.9.9")
    assert _pinned_harbor_requirement() == "harbor==9.9.9"


def test_requirement_none_when_harbor_absent(monkeypatch):
    from importlib.metadata import PackageNotFoundError

    monkeypatch.setattr(claude_code_agent, "_installed_harbor_git_pin", lambda: None)

    def _boom(_name):
        raise PackageNotFoundError("harbor")

    monkeypatch.setattr(claude_code_agent, "version", _boom)
    assert _pinned_harbor_requirement() is None


@pytest.mark.asyncio
async def test_install_runs_pip_with_git_requirement(tmp_path, monkeypatch):
    agent = OddishClaudeCode(logs_dir=tmp_path)

    calls: list[str] = []

    async def _fake_super_install(self, environment):
        calls.append("super")

    async def _fake_exec(self, environment, *, command):
        calls.append(command)

    monkeypatch.setattr(
        "harbor.agents.installed.claude_code.ClaudeCode.install", _fake_super_install
    )
    monkeypatch.setattr(OddishClaudeCode, "exec_as_agent", _fake_exec)

    await agent.install(environment=object())

    assert calls[0] == "super"  # stock CLI install runs first
    install_cmd = calls[1]
    assert install_cmd.startswith("pip install --user --quiet ")
    assert f"harbor @ git+{HARBOR_DEFAULT_SOURCE}@{HARBOR_DEFAULT_SHA}" in install_cmd


@pytest.mark.asyncio
async def test_install_is_best_effort_on_failure(tmp_path, monkeypatch):
    agent = OddishClaudeCode(logs_dir=tmp_path)

    async def _fake_super_install(self, environment):
        pass

    async def _boom_exec(self, environment, *, command):
        raise RuntimeError("sandbox pip is down")

    monkeypatch.setattr(
        "harbor.agents.installed.claude_code.ClaudeCode.install", _fake_super_install
    )
    monkeypatch.setattr(OddishClaudeCode, "exec_as_agent", _boom_exec)

    # Must NOT raise -- a harbor-install failure can't be allowed to fail the
    # whole probe trial.
    await agent.install(environment=object())
