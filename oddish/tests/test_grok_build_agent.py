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
    assert len(agent_commands) == 1
    command = agent_commands[0]
    assert "SENTINEL_START" not in command
    assert "payload-line" not in command
    assert f'"$(cat {_PROMPT_PATH})"' in command
    assert len(command.encode("utf-8")) < _MODAL_ARG_MAX_BYTES
