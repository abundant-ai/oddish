"""Oddish Claude Code wrappers: stdin prompts and probe Harbor install."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from harbor.agents.installed.base import (
    AgentAuthenticationError,
    ApiConnectionClosedError,
    ApiOverloadedError,
    NetworkConnectionError,
    UnknownApiError,
)
from harbor.agents.installed.claude_code import ClaudeCode

from oddish.config import HARBOR_DEFAULT_SHA, HARBOR_DEFAULT_SOURCE
from oddish.workers.agents import claude_code as claude_code_agent
from oddish.workers.agents.claude_code import (
    OddishClaudeCode,
    OddishProbeClaudeCode,
    _pinned_harbor_requirement,
)
from oddish.workers.harbor.failure_info import (
    PROVIDER_FAILURE_FILENAME,
    ProviderFailureEvidence,
    classify_provider_failure,
)


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


@pytest.mark.asyncio
async def test_install_retries_curl_56_with_bounded_exponential_backoff(
    tmp_path, monkeypatch
):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    attempts = 0
    delays: list[float] = []

    async def _install(_self, _environment):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise NetworkConnectionError("curl: (56) unexpected eof", return_code=56)

    async def _sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ClaudeCode, "install", _install)
    monkeypatch.setattr(claude_code_agent.asyncio, "sleep", _sleep)

    await agent.install(environment=object())

    assert attempts == 3
    assert delays == [2.0, 4.0]


@pytest.mark.asyncio
async def test_install_does_not_retry_non_transport_http_failure(tmp_path, monkeypatch):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    attempts = 0

    async def _install(_self, _environment):
        nonlocal attempts
        attempts += 1
        raise NetworkConnectionError("curl: (22) HTTP 403", return_code=22)

    monkeypatch.setattr(ClaudeCode, "install", _install)

    with pytest.raises(NetworkConnectionError):
        await agent.install(environment=object())

    assert attempts == 1


def test_structured_529_is_classified_as_overload(tmp_path):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    result = SimpleNamespace(
        return_code=1,
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "terminal_reason": "api_error",
                "api_error_status": 529,
                "session_id": "session-1",
                "request_id": "req-1",
            }
        ),
        stderr="",
    )

    error = agent._classify_exec_error("claude --print", result)

    assert isinstance(error, ApiOverloadedError)
    assert "http_status=529" in str(error)
    assert "request_id=req-1" in str(error)
    assert "session_id=session-1" in str(error)


def test_unstatused_overload_uses_harbor_text_classification(tmp_path):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    result = SimpleNamespace(
        return_code=1,
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "terminal_reason": "api_error",
                "session_id": "session-1",
                "result": "API Error: Overloaded",
            }
        ),
        stderr="",
    )

    error = agent._classify_exec_error("claude --print", result)

    assert isinstance(error, ApiOverloadedError)
    assert agent._last_provider_failure is not None
    assert agent._last_provider_decision is not None
    assert agent._last_provider_decision.retryable is True


def test_unstatused_authentication_error_stays_permanent(tmp_path):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    result = SimpleNamespace(
        return_code=1,
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "terminal_reason": "api_error",
                "session_id": "session-1",
                "result": "API Error: Not logged in",
            }
        ),
        stderr="",
    )

    error = agent._classify_exec_error("claude --print", result)

    assert isinstance(error, AgentAuthenticationError)


@pytest.mark.asyncio
async def test_provider_overload_resumes_same_session_and_honors_retry_hint(
    tmp_path, monkeypatch
):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    calls: list[tuple[str, bool, str | None, bool]] = []
    delays: list[float] = []

    async def _run(_self, instruction, _environment, _context):
        calls.append(
            (
                instruction,
                agent._resume,
                agent._resume_session_id,
                agent._append_stream_log,
            )
        )
        if len(calls) == 1:
            agent._last_provider_failure = ProviderFailureEvidence(
                provider="claude-code",
                terminal_reason="api_error",
                http_status=529,
                request_id="req-1",
                resume_token="session-1",
                retry_after_seconds=17.0,
            )
            agent._last_provider_decision = classify_provider_failure(
                agent._last_provider_failure,
                exception_type="ApiOverloadedError",
            )
            raise ApiOverloadedError("overloaded", return_code=1)

    async def _sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ClaudeCode, "run", _run)
    monkeypatch.setattr(claude_code_agent.asyncio, "sleep", _sleep)

    await agent.run("original task", environment=object(), context=object())

    assert delays == [17.0]
    assert calls[0] == ("original task", False, None, False)
    assert calls[1] == (
        claude_code_agent._RESUME_PROMPT,
        True,
        "session-1",
        True,
    )
    assert agent._resume is False
    assert agent._resume_session_id is None
    assert agent._append_stream_log is False


@pytest.mark.asyncio
async def test_multiple_provider_resumes_keep_first_session_id(tmp_path, monkeypatch):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    sessions: list[str | None] = []
    delays: list[float] = []

    async def _run(_self, _instruction, _environment, _context):
        sessions.append(agent._resume_session_id)
        if len(sessions) <= 2:
            evidence = ProviderFailureEvidence(
                provider="claude-code",
                terminal_reason="api_error",
                http_status=529,
                resume_token=f"session-{len(sessions)}",
            )
            agent._last_provider_failure = evidence
            agent._last_provider_decision = classify_provider_failure(
                evidence,
                exception_type="ApiOverloadedError",
            )
            raise ApiOverloadedError("overloaded", return_code=1)

    async def _sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ClaudeCode, "run", _run)
    monkeypatch.setattr(claude_code_agent.asyncio, "sleep", _sleep)

    await agent.run("original task", environment=object(), context=object())

    assert sessions == [None, "session-1", "session-1"]
    assert delays == [60.0, 120.0]


@pytest.mark.asyncio
async def test_unstatused_typed_connection_error_resumes_same_session(
    tmp_path, monkeypatch
):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    calls = 0
    delays: list[float] = []

    async def _run(_self, _instruction, _environment, _context):
        nonlocal calls
        calls += 1
        if calls == 1:
            agent._last_provider_failure = ProviderFailureEvidence(
                provider="claude-code",
                terminal_reason="api_error",
                resume_token="session-unknown",
                summary="API Error: Connection closed mid-response",
            )
            agent._last_provider_decision = classify_provider_failure(
                agent._last_provider_failure,
                exception_type="ApiConnectionClosedError",
            )
            raise ApiConnectionClosedError("provider API error", return_code=1)

    async def _sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ClaudeCode, "run", _run)
    monkeypatch.setattr(claude_code_agent.asyncio, "sleep", _sleep)

    await agent.run("original task", environment=object(), context=object())

    assert calls == 2
    assert delays == [60.0]


@pytest.mark.asyncio
async def test_permanent_unknown_api_error_does_not_resume(tmp_path, monkeypatch):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    calls = 0
    delays: list[float] = []

    async def _run(_self, _instruction, _environment, _context):
        nonlocal calls
        calls += 1
        agent._last_provider_failure = ProviderFailureEvidence(
            provider="claude-code",
            terminal_reason="api_error",
            http_status=400,
            resume_token="session-invalid-request",
            summary="API Error: 400 invalid request",
        )
        agent._last_provider_decision = classify_provider_failure(
            agent._last_provider_failure,
            exception_type="UnknownApiError",
        )
        raise UnknownApiError("invalid request", return_code=1)

    async def _sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(ClaudeCode, "run", _run)
    monkeypatch.setattr(claude_code_agent.asyncio, "sleep", _sleep)

    with pytest.raises(UnknownApiError):
        await agent.run("original task", environment=object(), context=object())

    assert calls == 1
    assert delays == []
    payload = json.loads((tmp_path / PROVIDER_FAILURE_FILENAME).read_text())
    assert payload["http_status"] == 400
    assert payload["resume_token"] == "session-invalid-request"


@pytest.mark.asyncio
async def test_final_generic_crash_does_not_persist_stale_resumed_failure(
    tmp_path, monkeypatch
):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    calls = 0

    async def _run(_self, _instruction, _environment, _context):
        nonlocal calls
        calls += 1
        if calls == 1:
            evidence = ProviderFailureEvidence(
                provider="claude-code",
                terminal_reason="api_error",
                http_status=529,
                resume_token="session-1",
            )
            agent._last_provider_failure = evidence
            agent._last_provider_decision = classify_provider_failure(
                evidence,
                exception_type="ApiOverloadedError",
            )
            raise ApiOverloadedError("overloaded", return_code=1)
        raise UnknownApiError("process exited without a result event", return_code=1)

    async def _sleep(_delay):
        return None

    monkeypatch.setattr(ClaudeCode, "run", _run)
    monkeypatch.setattr(claude_code_agent.asyncio, "sleep", _sleep)

    with pytest.raises(UnknownApiError):
        await agent.run("original task", environment=object(), context=object())

    assert calls == 2
    assert not (tmp_path / PROVIDER_FAILURE_FILENAME).exists()


@pytest.mark.asyncio
async def test_success_clears_provider_failure_from_previous_logical_run(
    tmp_path, monkeypatch
):
    sidecar = tmp_path / PROVIDER_FAILURE_FILENAME
    sidecar.write_text('{"stale": true}', encoding="utf-8")
    agent = OddishClaudeCode(logs_dir=tmp_path)

    async def _run(_self, _instruction, _environment, _context):
        return None

    monkeypatch.setattr(ClaudeCode, "run", _run)

    await agent.run("original task", environment=object(), context=object())

    assert not sidecar.exists()


def test_resume_command_uses_exact_session_and_appends_stream_log(tmp_path):
    agent = OddishClaudeCode(logs_dir=tmp_path)
    agent._resume = True
    agent._resume_session_id = "session-1"
    agent._append_stream_log = True

    command = agent._build_claude_command("ignored", "--continue ")

    assert "--resume session-1" in command
    assert "--continue" not in command
    assert "| tee -a /logs/agent/claude-code.txt" in command


def test_cost_parser_sums_each_process_result_after_resume(tmp_path):
    stream = tmp_path / "claude-code.txt"
    stream.write_text(
        "\n".join(
            [
                json.dumps({"type": "result", "total_cost_usd": 1.25}),
                json.dumps({"type": "result", "total_cost_usd": 3.75}),
            ]
        ),
        encoding="utf-8",
    )
    agent = OddishClaudeCode(logs_dir=tmp_path)

    assert agent._parse_total_cost_from_stream_json() == 5.0
