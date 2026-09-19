"""Oddish Claude Code wrappers: stdin prompts and probe Harbor install."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harbor.agents.installed.base import ApiOverloadedError
from harbor.environments.base import ExecResult
from harbor.models.agent.context import AgentContext

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE
from oddish.workers.agents import claude_code as claude_code_agent
from oddish.workers.agents.claude_code import (
    OddishClaudeCode,
    OddishProbeClaudeCode,
    _pinned_harbor_requirement,
    convert_claude_code_stream_text_to_trajectory,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", [OddishClaudeCode, OddishProbeClaudeCode])
@pytest.mark.parametrize("session_key", ["session_id", "sessionId"])
async def test_successful_steps_resume_the_same_claude_session(
    tmp_path, agent_type, session_key
):
    agent = agent_type(logs_dir=tmp_path)
    output = "\n".join(
        [
            "non-JSON progress",
            "[]",
            json.dumps({"type": "system", session_key: "step-session"}),
            json.dumps({"type": "result", session_key: "step-session"}),
        ]
    )

    async def execute(*, command, **kwargs):
        return ExecResult(
            return_code=0,
            stdout=output if "| claude " in command else "",
            stderr="",
        )

    environment = SimpleNamespace(exec=AsyncMock(side_effect=execute))
    await agent.run("Step one", environment, AgentContext())
    for instruction in ("Step two", "Step three"):
        await agent.resume(instruction, environment, AgentContext())
        command = environment.exec.call_args.kwargs["command"]
        assert "--resume step-session" in command
        assert "--continue" not in command
        assert "tee -a /logs/agent/claude-code.txt" in command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stdout,stderr",
    [
        ('{"type":"result"}', ""),
        ('{"session_id":"one"}', '{"sessionId":"two"}'),
        ('{"session_id":"one","sessionId":"two"}', ""),
    ],
)
async def test_success_without_a_unique_session_still_refuses_resume(
    tmp_path, stdout, stderr
):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    environment = SimpleNamespace(
        exec=AsyncMock(
            return_value=ExecResult(return_code=0, stdout=stdout, stderr=stderr)
        )
    )
    await agent.run("Step one", environment, AgentContext())
    environment.exec.reset_mock()
    with pytest.raises(RuntimeError, match="did not emit a unique session id"):
        await agent.resume("Step two", environment, AgentContext())
    environment.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_resumed_success_cannot_switch_to_a_different_session(tmp_path):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    environment = SimpleNamespace(
        exec=AsyncMock(
            return_value=ExecResult(return_code=0, stdout='{"session_id":"one"}')
        )
    )
    await agent.run("Step one", environment, AgentContext())
    environment.exec.return_value = ExecResult(
        return_code=0, stdout='{"session_id":"two"}'
    )
    await agent.resume("Step two", environment, AgentContext())
    environment.exec.reset_mock()
    with pytest.raises(RuntimeError, match="did not emit a unique session id"):
        await agent.resume("Step three", environment, AgentContext())
    environment.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_run_discards_the_previous_successful_session(tmp_path):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    environment = SimpleNamespace(
        exec=AsyncMock(
            return_value=ExecResult(return_code=0, stdout='{"session_id":"one"}')
        )
    )
    await agent.run("First task", environment, AgentContext())
    environment.exec.return_value = ExecResult(return_code=0, stdout="")
    await agent.run("Unrelated task", environment, AgentContext())
    environment.exec.reset_mock()
    with pytest.raises(RuntimeError, match="did not emit a unique session id"):
        await agent.resume("Continue unrelated task", environment, AgentContext())
    environment.exec.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_error_still_preserves_harbor_retry_session(tmp_path):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    environment = SimpleNamespace(
        exec=AsyncMock(
            return_value=ExecResult(
                return_code=1,
                stdout='{"type":"system","session_id":"retry-session"}\nAPI Error: Overloaded',
            )
        )
    )
    with pytest.raises(ApiOverloadedError):
        await agent._exec(environment, "claude -p test")
    environment.exec.return_value = ExecResult(return_code=0, stdout="")
    await agent.resume("Continue after provider error", environment, AgentContext())
    assert "--resume retry-session" in environment.exec.call_args.kwargs["command"]


def test_convert_claude_code_stream_text_to_trajectory_recovers_atif():
    stream = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "system",
                "subtype": "init",
                "session_id": "session-1",
                "model": "claude-test",
                "tools": [],
            },
            {
                "type": "assistant",
                "session_id": "session-1",
                "message": {
                    "id": "message-1",
                    "role": "assistant",
                    "model": "claude-test",
                    "content": [{"type": "text", "text": "Recovered."}],
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                },
            },
            {
                "type": "result",
                "session_id": "session-1",
                "is_error": False,
                "result": "Recovered.",
                "total_cost_usd": 0.01,
            },
        )
    )

    trajectory = convert_claude_code_stream_text_to_trajectory(
        stream,
        model_name="anthropic/claude-test",
    )

    assert trajectory is not None
    assert trajectory["session_id"] == "session-1"
    assert [step["message"] for step in trajectory["steps"]] == ["Recovered."]


@pytest.mark.asyncio
async def test_run_delivers_exact_prompt_over_stdin_without_plaintext_on_argv(
    tmp_path, monkeypatch
):
    """Harbor -- not Oddish -- keeps the prompt off ``claude``'s argv.

    Oddish used to force this by overriding Harbor's ``_build_claude_command``
    seam; Harbor now does it natively, so this pins the *inherited* guarantees
    rather than a command string Oddish builds:

    * the task text never reaches ``claude``'s command line. Long-horizon tasks
      restart services with ``pkill -f``, and a service name lifted from an
      argv-borne prompt can match and kill the agent itself;
    * the prompt still arrives verbatim (exact UTF-8, quotes and newlines
      intact), piped in from a transient env var that is unset before the agent
      starts;
    * the stream is teed to ``/logs/agent/claude-code.txt``, the path live
      tailing, trajectory parsing, probe analysis and cost parsing read back.
    """
    agent = OddishClaudeCode(logs_dir=tmp_path, reasoning_effort="xhigh")
    instruction = "restart rj-rust; preserve 'quotes' and\nnewlines"

    calls: list[tuple[str, dict[str, str]]] = []

    async def _fake_exec(self, environment, *, command, env=None, **_kwargs):
        calls.append((command, dict(env or {})))

    monkeypatch.setattr(OddishClaudeCode, "exec_as_agent", _fake_exec)

    await agent.run(instruction, environment=object(), context=object())

    command, env = calls[-1]

    # Off argv: no plaintext prompt, and no `--print -- <instruction>` form.
    assert instruction not in command
    assert "--print --" not in command

    # Delivered verbatim through exactly one transient env var...
    carriers = [name for name, value in env.items() if value == instruction]
    assert len(carriers) == 1
    carrier = carriers[0]
    # ...which is unset before `claude` runs, then piped onto its stdin.
    assert f"unset {carrier}" in command
    assert 'printf "%s"' in command
    assert "| claude --verbose --output-format=stream-json" in command

    # Agent kwargs still render as CLI flags.
    assert "--effort xhigh" in command

    # The log five downstream consumers read back.
    assert "| tee /logs/agent/claude-code.txt" in command


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
