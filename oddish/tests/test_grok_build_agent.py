"""OddishGrokBuild: keep the instruction out of the Modal exec argv.

Modal rejects any sandbox ``exec`` whose CMD arguments exceed 65536 bytes
(ARG_MAX). The agent used to inline the (up to 3x embedded) task instruction
into ``grok -p <instruction>``, so large tasks failed at agent start with
``InvalidError: Total length of CMD arguments cannot exceed 65536 bytes``.
The instruction is now uploaded as a file and read back via ``"$(cat ...)"``.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from oddish.workers.agents import grok_build as grok_build_module
from oddish.workers.agents.grok_build import (
    _PROMPT_PATH,
    _XAI_API_KEY_ENV,
    _XAI_API_KEYS_ENV,
    OddishGrokBuild,
)


def test_advertises_atif_support():
    assert OddishGrokBuild.SUPPORTS_ATIF is True


def test_build_config_toml_defaults_to_responses_backend(tmp_path):
    """Without an override the upstream Harbor default is preserved verbatim."""
    agent = OddishGrokBuild(logs_dir=tmp_path, model_name="xai/v9-stickynote")
    config = agent.build_config_toml()
    assert 'api_backend = "responses"' in config
    assert "chat_completions" not in config


def test_build_config_toml_applies_api_backend_override(tmp_path):
    """``api_backend`` rewrites every model block's transport."""
    agent = OddishGrokBuild(
        logs_dir=tmp_path,
        model_name="xai/v9-stickynote",
        api_backend="chat_completions",
    )
    config = agent.build_config_toml()
    # Both the model-specific and the [model.grok-build] blocks are switched.
    assert config.count('api_backend = "chat_completions"') == 2
    assert 'api_backend = "responses"' not in config
    # The requested model still routes through, untouched by the rewrite.
    assert "v9-stickynote" in config


def test_invalid_api_backend_rejected(tmp_path):
    with pytest.raises(ValueError, match="api_backend"):
        OddishGrokBuild(
            logs_dir=tmp_path,
            model_name="xai/v9-stickynote",
            api_backend="bogus-endpoint",
        )


def test_build_config_toml_omits_reliability_keys_by_default(tmp_path):
    config = OddishGrokBuild(
        logs_dir=tmp_path, model_name="xai/v9-stickynote"
    ).build_config_toml()
    assert "max_retries" not in config
    assert "inference_idle_timeout_secs" not in config


def test_build_config_toml_applies_reliability_keys_to_every_model_block(tmp_path):
    # --agent-kwarg delivers values as strings; ints must work too.
    agent = OddishGrokBuild(
        logs_dir=tmp_path,
        model_name="xai/v9-stickynote",
        max_retries="15",
        inference_idle_timeout_secs=300,
    )
    parsed = tomllib.loads(agent.build_config_toml())
    for table in ("v9-stickynote", "grok-build"):
        assert parsed["model"][table]["max_retries"] == 15
        assert parsed["model"][table]["inference_idle_timeout_secs"] == 300


@pytest.mark.parametrize("bad", ["nope", "0", -3, "12.5"])
def test_invalid_reliability_kwargs_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="max_retries"):
        OddishGrokBuild(logs_dir=tmp_path, max_retries=bad)
    with pytest.raises(ValueError, match="inference_idle_timeout_secs"):
        OddishGrokBuild(logs_dir=tmp_path, inference_idle_timeout_secs=bad)


# Comfortably larger than ARG_MAX and than a single realistic instruction; the
# old code embedded this three times, so the exec argv would have been ~600KB.
_LARGE_INSTRUCTION = "SENTINEL_START " + ("payload-line\n" * 20_000) + " SENTINEL_END"
_MODAL_ARG_MAX_BYTES = 65536


@pytest.mark.asyncio
async def test_run_uploads_prompt_and_keeps_exec_command_small(tmp_path, monkeypatch):
    agent = OddishGrokBuild(logs_dir=tmp_path)

    uploads: list[tuple[str, str]] = []
    uploaded_content: dict[str, str] = {}
    root_commands: list[str] = []
    agent_commands: list[str] = []

    class _FakeEnv:
        async def upload_file(self, source_path, target_path):
            uploads.append((str(source_path), target_path))
            uploaded_content[target_path] = Path(source_path).read_text(
                encoding="utf-8"
            )

    async def _fake_write_config(self, environment):
        return None

    async def _fake_exec_as_root(self, environment, *, command, **kwargs):
        root_commands.append(command)

    async def _fake_exec_as_agent(self, environment, *, command, **kwargs):
        agent_commands.append(command)

    monkeypatch.setattr(OddishGrokBuild, "_write_config", _fake_write_config)
    monkeypatch.setattr(OddishGrokBuild, "exec_as_root", _fake_exec_as_root)
    monkeypatch.setattr(OddishGrokBuild, "exec_as_agent", _fake_exec_as_agent)

    await agent.run(
        instruction=_LARGE_INSTRUCTION,
        environment=_FakeEnv(),
        context=object(),
    )

    # The full instruction is transferred out-of-band, to the expected path.
    assert uploads == [(uploads[0][0], _PROMPT_PATH)]
    assert "SENTINEL_START" in uploaded_content[_PROMPT_PATH]
    assert "SENTINEL_END" in uploaded_content[_PROMPT_PATH]

    # The uploaded file is made readable for a non-root agent user.
    assert any("chmod" in c and _PROMPT_PATH in c for c in root_commands)

    # Exactly one agent exec runs grok, and it never carries the instruction
    # body -- it reads the staged file instead, so it stays well under ARG_MAX.
    # (A second agent exec copies the grok session store into the trial logs.)
    grok_commands = [c for c in agent_commands if "grok -p" in c]
    assert len(grok_commands) == 1
    command = grok_commands[0]
    assert "SENTINEL_START" not in command
    assert "payload-line" not in command
    assert f'"$(cat {_PROMPT_PATH})"' in command
    assert "--reasoning-effort high" in command
    assert len(command.encode("utf-8")) < _MODAL_ARG_MAX_BYTES

    # The session store is captured out-of-band so tool calls + token usage
    # (absent from the text-only stdout) survive sandbox teardown.
    assert any("grok-session" in c for c in agent_commands)

    # An idle-timeout or rate-limit death resumes the session (bounded) instead
    # of failing the trial: both are fatal to the CLI yet transient server-side,
    # the stream watchdog because a fresh request lands on a healthy replica and
    # the rate limit because xAI's buckets refill. One resume arm per fallback
    # variant, so the resume replays whichever flag set actually ran.
    assert (
        "grep -Eqi '(idle timeout|rate limit|rate_limit|too many requests|429)'"
        in command
    )
    assert command.count("grok -c -p") == 6
    assert "resumes -lt 3" in command
    # The resume appends to the streamed event log and re-sends a short inline
    # continuation, never the staged instruction.
    assert command.count(">>/logs/agent/grok-build.json") == 6
    assert "Continue the original task" in command


async def _generated_command(tmp_path, monkeypatch) -> str:
    agent = OddishGrokBuild(logs_dir=tmp_path)
    agent_commands: list[str] = []

    class _FakeEnv:
        async def upload_file(self, source_path, target_path):
            return None

    async def _noop(self, environment, **kwargs):
        return None

    async def _record(self, environment, *, command, **kwargs):
        agent_commands.append(command)

    monkeypatch.setattr(OddishGrokBuild, "_write_config", _noop)
    monkeypatch.setattr(OddishGrokBuild, "exec_as_root", _record)
    monkeypatch.setattr(OddishGrokBuild, "exec_as_agent", _record)
    await agent.run(instruction="task", environment=_FakeEnv(), context=object())
    return next(c for c in agent_commands if "grok -p" in c)


def _run_in_shell(command: str, tmp_path, stub_body: str):
    """Execute the generated command under bash against a stubbed ``grok``.

    The stub lands in ``$HOME/.local/bin`` of a throwaway HOME, which the
    command's own ``export PATH`` puts first; ``/logs/agent`` is rewritten to a
    writable dir. This pins the shell state machine itself (fallback routing,
    resume trigger, loop bound), not just the command text.
    """
    home = tmp_path / "home"
    logdir = tmp_path / "logs"
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    logdir.mkdir()
    stub = bindir / "grok"
    stub.write_text("#!/bin/bash\n" + stub_body, encoding="utf-8")
    stub.chmod(0o755)
    (tmp_path / "prompt.txt").write_text("task", encoding="utf-8")
    rewritten = command.replace("/logs/agent", str(logdir)).replace(
        _PROMPT_PATH, str(tmp_path / "prompt.txt")
    )
    proc = subprocess.run(
        ["bash", "-c", rewritten],
        env={"HOME": str(home), "LOGDIR": str(logdir), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = (logdir / "calls.log").read_text(encoding="utf-8").splitlines()
    stdout = (logdir / "grok-build.json").read_text(encoding="utf-8")
    return proc.returncode, calls, stdout


_IDLE_TIMEOUT_STDERR = (
    'Error: Internal error: { "message": '
    '"inference idle timeout after 600s with no chunks" }'
)

_STALL_TWICE_STUB = f"""
echo "$*" >> "$LOGDIR/calls.log"
n=$(wc -l < "$LOGDIR/calls.log")
if [ "$n" -le 2 ]; then
  echo '{{"type":"text","data":"partial"}}'
  echo '{_IDLE_TIMEOUT_STDERR}' >&2
  exit 1
fi
echo '{{"type":"text","data":"resumed"}}'
exit 0
"""

_RATE_LIMIT_STUB = """
echo "$*" >> "$LOGDIR/calls.log"
echo "Error: You've hit your team's API rate limit." >&2
exit 1
"""

# Matches none of the fallback greps and neither resume pattern, so the shell
# must give up on the first death rather than replaying it.
_UNKNOWN_ERROR_STUB = """
echo "$*" >> "$LOGDIR/calls.log"
echo "Error: the model is not available to your team." >&2
exit 1
"""

_FALLBACK_THEN_STALL_STUB = f"""
echo "$*" >> "$LOGDIR/calls.log"
case "$*" in
  *--reasoning-effort*) echo "unknown option '--reasoning-effort'" >&2; exit 1;;
esac
if [ ! -f "$LOGDIR/stalled" ]; then
  touch "$LOGDIR/stalled"
  echo '{_IDLE_TIMEOUT_STDERR}' >&2
  exit 1
fi
echo '{{"type":"text","data":"resumed"}}'
exit 0
"""


@pytest.mark.asyncio
async def test_shell_resumes_after_idle_timeout(tmp_path, monkeypatch):
    command = await _generated_command(tmp_path, monkeypatch)
    rc, calls, stdout = _run_in_shell(command, tmp_path, _STALL_TWICE_STUB)
    assert rc == 0
    # Primary run + two resumes, each streaming into the same event log.
    assert len(calls) == 3
    assert all("-c" in c.split() for c in calls[1:])
    assert stdout.count('"partial"') == 2
    assert '"resumed"' in stdout


@pytest.mark.asyncio
async def test_shell_does_not_resume_other_failures(tmp_path, monkeypatch):
    command = await _generated_command(tmp_path, monkeypatch)
    rc, calls, _ = _run_in_shell(command, tmp_path, _UNKNOWN_ERROR_STUB)
    assert rc != 0
    # A death matching no fallback grep and neither resume pattern must not loop.
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_shell_resumes_rate_limit(tmp_path, monkeypatch):
    """A rate-limited run resumes instead of throwing the trial away.

    An xAI rate limit kills the CLI mid-run, discarding every turn already
    spent. The throttle is on the account, so the loop waits (see the backoff
    below, zeroed here) before each replay rather than re-hitting the limit
    immediately. A limit that never clears still exits non-zero, as before.
    """
    monkeypatch.setattr(grok_build_module, "_RATE_LIMIT_BACKOFF_SEC", 0)
    command = await _generated_command(tmp_path, monkeypatch)
    assert "delay=0;" in command
    rc, calls, _ = _run_in_shell(command, tmp_path, _RATE_LIMIT_STUB)
    assert rc != 0
    # Initial arm + the three bounded resumes; a stub that is always limited
    # exhausts the budget rather than looping forever.
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_rate_limit_resume_waits_between_attempts(tmp_path, monkeypatch):
    """The backoff is real: resuming into a live throttle just re-hits it.

    Pins the doubling (60s, 120s, 240s) so a future edit cannot silently turn
    the resume budget into three retries spent inside a second.
    """
    command = await _generated_command(tmp_path, monkeypatch)
    assert "delay=60;" in command
    assert 'sleep "$delay"; delay=$((delay*2));' in command


@pytest.mark.asyncio
async def test_shell_resume_replays_the_variant_that_ran(tmp_path, monkeypatch):
    command = await _generated_command(tmp_path, monkeypatch)
    rc, calls, stdout = _run_in_shell(command, tmp_path, _FALLBACK_THEN_STALL_STUB)
    assert rc == 0
    # arm0 (rejected flag) -> arm1 fallback (stalls) -> resume of arm1.
    assert len(calls) == 3
    assert "--reasoning-effort" not in calls[2]
    assert "-c" in calls[2].split()
    assert '"resumed"' in stdout


@pytest.mark.asyncio
async def test_session_captured_even_when_agent_fails(tmp_path, monkeypatch):
    """A crashed grok run must still upload its session store.

    Without this, idle-timeout deaths leave no unified.jsonl in the trial logs
    and the only diagnostic evidence is the terse stderr JSON.
    """
    agent = OddishGrokBuild(logs_dir=tmp_path)
    agent_commands: list[str] = []

    class _FakeEnv:
        async def upload_file(self, source_path, target_path):
            return None

    async def _fake_write_config(self, environment):
        return None

    async def _fake_exec_as_root(self, environment, *, command, **kwargs):
        return None

    async def _fake_exec_as_agent(self, environment, *, command, **kwargs):
        agent_commands.append(command)
        if "grok -p" in command:
            raise RuntimeError("Command failed (exit 1)")

    monkeypatch.setattr(OddishGrokBuild, "_write_config", _fake_write_config)
    monkeypatch.setattr(OddishGrokBuild, "exec_as_root", _fake_exec_as_root)
    monkeypatch.setattr(OddishGrokBuild, "exec_as_agent", _fake_exec_as_agent)

    with pytest.raises(RuntimeError):
        await agent.run(
            instruction="do the thing", environment=_FakeEnv(), context=object()
        )

    assert any("grok-session" in c for c in agent_commands)


def _agent_with_no_xai_env(tmp_path, monkeypatch) -> OddishGrokBuild:
    """Build an agent with both key vars unset.

    ``_get_env`` falls through to ``os.environ``, and a dev machine running
    these tests usually has a real ``XAI_API_KEY`` exported. Clearing both vars
    keeps every key-pool assertion about what the test set, not the ambient
    shell.
    """
    monkeypatch.delenv(_XAI_API_KEY_ENV, raising=False)
    monkeypatch.delenv(_XAI_API_KEYS_ENV, raising=False)
    return OddishGrokBuild(logs_dir=tmp_path, model_name="xai/v9-stickynote")


def test_xai_env_draws_from_the_key_pool(tmp_path, monkeypatch):
    """A pool spreads concurrent trials across keys, one draw per trial."""
    agent = _agent_with_no_xai_env(tmp_path, monkeypatch)
    monkeypatch.setenv(_XAI_API_KEYS_ENV, "key_A,key_B")
    assert agent._xai_env()[_XAI_API_KEY_ENV] in {"key_A", "key_B"}


def test_xai_env_reuses_the_first_drawn_key(tmp_path, monkeypatch):
    """One key per trial, not per call.

    ``_xai_env`` feeds both the config write and the run exec. If each call
    re-drew, a trial could authenticate its config against one account and its
    grok run against another -- and a resumed session would wander between keys
    mid-run. The draw must happen once and stick.
    """
    agent = _agent_with_no_xai_env(tmp_path, monkeypatch)
    monkeypatch.setenv(_XAI_API_KEYS_ENV, "key_A,key_B")

    draws: list[list[str]] = []

    def _fake_choice(pool):
        draws.append(list(pool))
        # Hand back a different key every call, so a lost memoization shows up
        # as a differing second key rather than as a coin flip.
        return pool[(len(draws) - 1) % len(pool)]

    monkeypatch.setattr(grok_build_module.random, "choice", _fake_choice)

    first = agent._xai_env()
    second = agent._xai_env()
    assert first == second
    assert len(draws) == 1


def test_xai_env_falls_back_to_the_single_key(tmp_path, monkeypatch):
    """Deployments without a pool keep working on the lone key var."""
    agent = _agent_with_no_xai_env(tmp_path, monkeypatch)
    monkeypatch.setenv(_XAI_API_KEY_ENV, "key_solo")
    assert agent._xai_env() == {_XAI_API_KEY_ENV: "key_solo"}


def test_pool_wins_over_the_single_key(tmp_path, monkeypatch):
    """The pool is the opt-in, so it must beat a leftover single key.

    Both vars are set in practice (the single key predates the pool), and
    honoring the old one would silently pin every trial to one account.
    """
    agent = _agent_with_no_xai_env(tmp_path, monkeypatch)
    monkeypatch.setenv(_XAI_API_KEY_ENV, "key_stale")
    monkeypatch.setenv(_XAI_API_KEYS_ENV, "key_A,key_B")
    assert agent._xai_env()[_XAI_API_KEY_ENV] in {"key_A", "key_B"}


def test_xai_env_empty_without_any_key(tmp_path, monkeypatch):
    """No key configured must stay absent, not become an empty ``XAI_API_KEY``."""
    agent = _agent_with_no_xai_env(tmp_path, monkeypatch)
    assert agent._xai_env() == {}


def test_pool_tolerates_whitespace_and_empty_entries(tmp_path, monkeypatch):
    """Hand-edited pool strings carry stray spaces and trailing commas.

    An unstripped entry becomes an ``XAI_API_KEY`` with a leading space and
    every request 401s, so the parse is pinned rather than the draw.
    """
    agent = _agent_with_no_xai_env(tmp_path, monkeypatch)
    monkeypatch.setenv(_XAI_API_KEYS_ENV, "key_A , , key_B")

    pools: list[list[str]] = []

    def _fake_choice(pool):
        pools.append(list(pool))
        return pool[0]

    monkeypatch.setattr(grok_build_module.random, "choice", _fake_choice)

    assert agent._xai_env() == {_XAI_API_KEY_ENV: "key_A"}
    assert set(pools[0]) == {"key_A", "key_B"}
