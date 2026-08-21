"""Oddish Claude Code wrappers: stdin prompts and probe Harbor install."""

from __future__ import annotations

import base64
import shlex
from types import SimpleNamespace

import pytest

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE
from oddish.workers.agents import claude_code as claude_code_agent
from oddish.workers.agents.claude_code import (
    OddishAnalysisClaudeCode,
    OddishClaudeCode,
    OddishProbeClaudeCode,
    _pinned_harbor_requirement,
)


class _AnalysisEnvironment:
    def __init__(self, validation_results: list[tuple[int, str]]):
        self.validation_results = list(validation_results)
        self.commands: list[str] = []

    async def exec(self, command, **_kwargs):
        self.commands.append(command)
        if "validate-analysis-result" in command:
            return_code, output = self.validation_results.pop(0)
            return SimpleNamespace(
                return_code=return_code,
                stdout=output,
                stderr="",
            )
        return SimpleNamespace(return_code=0, stdout="", stderr="")


def test_command_delivers_exact_prompt_over_stdin_without_plaintext_on_argv(tmp_path):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    instruction = "restart rj-rust; preserve 'quotes' and\nnewlines"

    command = agent._build_claude_command(shlex.quote(instruction), "--effort xhigh ")

    assert instruction not in command
    assert "--print --" not in command
    assert "| base64 -d | claude " in command
    assert "--effort xhigh --print" in command

    encoded_shell_word = command.split("printf %s ", 1)[1].split(" | base64 -d", 1)[0]
    encoded = shlex.split(encoded_shell_word)[0]
    assert base64.b64decode(encoded).decode("utf-8") == instruction


@pytest.mark.asyncio
async def test_analysis_agent_accepts_valid_first_pass(tmp_path, monkeypatch):
    agent = OddishAnalysisClaudeCode(
        logs_dir=tmp_path,
        extra_env={"ODDISH_ANALYSIS_ARTIFACT": "qa_result.json"},
    )
    environment = _AnalysisEnvironment([(0, "")])
    runs: list[tuple[str, bool]] = []

    async def _fake_run(self, instruction, environment, context):
        runs.append((instruction, self._resume))

    monkeypatch.setattr(OddishClaudeCode, "run", _fake_run)

    await agent.run("analyze", environment, SimpleNamespace())

    assert runs == [("analyze", False)]
    assert "analysis-attempts/qa_result.attempt-1.json" in environment.commands[0]


@pytest.mark.asyncio
async def test_analysis_agent_repairs_in_same_session(tmp_path, monkeypatch):
    agent = OddishAnalysisClaudeCode(
        logs_dir=tmp_path,
        extra_env={"ODDISH_ANALYSIS_ARTIFACT": "qa_result.json"},
    )
    environment = _AnalysisEnvironment(
        [(1, "trials[0].analysis.evidence must be a string"), (0, "")]
    )
    runs: list[tuple[str, bool]] = []

    async def _fake_run(self, instruction, environment, context):
        runs.append((instruction, self._resume))

    monkeypatch.setattr(OddishClaudeCode, "run", _fake_run)

    await agent.run("analyze", environment, SimpleNamespace())

    assert len(runs) == 2
    assert runs[0] == ("analyze", False)
    repair_prompt, resumed = runs[1]
    assert resumed is True
    assert "Do not repeat the analysis" in repair_prompt
    assert "evidence must be a string" in repair_prompt
    assert "/probe-harness/expected.json" in repair_prompt
    assert "analysis-attempts/qa_result.attempt-2.json" in environment.commands[-1]
    assert agent._resume is False


@pytest.mark.asyncio
async def test_analysis_agent_stops_after_two_failed_repairs(tmp_path, monkeypatch):
    agent = OddishAnalysisClaudeCode(
        logs_dir=tmp_path,
        extra_env={"ODDISH_ANALYSIS_ARTIFACT": "qa_result.json"},
    )
    environment = _AnalysisEnvironment(
        [(1, "invalid one"), (1, "invalid two"), (1, "invalid three")]
    )
    runs: list[tuple[str, bool]] = []

    async def _fake_run(self, instruction, environment, context):
        runs.append((instruction, self._resume))

    monkeypatch.setattr(OddishClaudeCode, "run", _fake_run)

    await agent.run("analyze", environment, SimpleNamespace())

    assert len(runs) == 3
    assert [resume for _, resume in runs] == [False, True, True]
    assert sum("validate-analysis-result" in c for c in environment.commands) == 3


def test_requirement_explicit_source_sha_is_no_git_tarball():
    # The sandbox has no git binary, so the requirement must be installable
    # over plain HTTPS: GitHub sources render as the commit tarball.
    req = _pinned_harbor_requirement("https://github.com/dot-agi/harbor", "a" * 40)
    assert req == (
        f"harbor @ https://github.com/dot-agi/harbor/archive/{'a' * 40}.tar.gz"
    )


def test_requirement_from_installed_direct_url_is_no_git_tarball():
    # The orchestrator's harbor is git-installed from the locked fork; the
    # requirement derived from its direct_url pins the same commit but as a
    # tarball the git-less sandbox can install.
    req = _pinned_harbor_requirement()
    assert req == (
        f"harbor @ {HARBOR_DEFAULT_SOURCE}/archive/{HARBOR_DEFAULT_SHA}.tar.gz"
    )


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
async def test_install_runs_pip_with_no_git_requirement(tmp_path, monkeypatch):
    agent = OddishProbeClaudeCode(logs_dir=tmp_path)

    calls: list[str] = []

    async def _fake_super_install(self, environment):
        calls.append("super")

    async def _fake_exec(self, environment, *, command):
        calls.append(command)

    monkeypatch.setattr(
        "harbor.agents.installed.claude_code.ClaudeCode.install", _fake_super_install
    )
    monkeypatch.setattr(OddishProbeClaudeCode, "exec_as_agent", _fake_exec)

    await agent.install(environment=object())

    assert calls[0] == "super"  # stock CLI install runs first
    install_cmd = calls[1]
    assert install_cmd.startswith("pip install --user --quiet ")
    assert (
        f"harbor @ {HARBOR_DEFAULT_SOURCE}/archive/{HARBOR_DEFAULT_SHA}.tar.gz"
        in install_cmd
    )
    assert "git+" not in install_cmd


@pytest.mark.asyncio
async def test_install_is_best_effort_on_failure(tmp_path, monkeypatch):
    agent = OddishProbeClaudeCode(logs_dir=tmp_path)

    async def _fake_super_install(self, environment):
        pass

    async def _boom_exec(self, environment, *, command):
        raise RuntimeError("sandbox pip is down")

    monkeypatch.setattr(
        "harbor.agents.installed.claude_code.ClaudeCode.install", _fake_super_install
    )
    monkeypatch.setattr(OddishProbeClaudeCode, "exec_as_agent", _boom_exec)

    # Must NOT raise -- a harbor-install failure can't be allowed to fail the
    # whole probe trial.
    await agent.install(environment=object())
