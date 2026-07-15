"""OddishGrokBuild: keep the instruction out of the Modal exec argv.

Modal rejects any sandbox ``exec`` whose CMD arguments exceed 65536 bytes
(ARG_MAX). The agent used to inline the (up to 3x embedded) task instruction
into ``grok -p <instruction>``, so large tasks failed at agent start with
``InvalidError: Total length of CMD arguments cannot exceed 65536 bytes``.
The instruction is now uploaded as a file and read back via ``"$(cat ...)"``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oddish.workers.agents.grok_build import _PROMPT_PATH, OddishGrokBuild


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
    config = agent.build_config_toml()
    assert config.count("max_retries = 15") == 2
    assert config.count("inference_idle_timeout_secs = 300") == 2


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

    # An idle-timeout death resumes the session (bounded) instead of failing
    # the trial: the xAI-side stream watchdog is the only grok failure that is
    # both fatal to the CLI and transient server-side.
    assert "grep -qi 'idle timeout'" in command
    assert command.count("grok -c -p") == 1
    assert "resumes -lt 3" in command
    # The resume appends to the streamed event log and re-sends a short inline
    # continuation, never the staged instruction.
    assert ">>/logs/agent/grok-build.json" in command
    assert "Continue the original task" in command


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
