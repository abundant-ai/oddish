"""ClaudeCliClient is a first-class analyzer backend.

Before this, CLAUDE_CLI was the one LLMClientType AnalyzerBlock could not
provision: create_llm_client raised "context-bound", and the post-trial
classifier smuggled a closure in through a client_factory argument. These tests
pin the native path -- config in, client out -- and the claude-code output
parsing both backends share.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from oddish.blocks.analyzer import claude_cli_client
from oddish.blocks.analyzer.analyzer_llm_client import (
    LLMClientType,
    create_llm_client,
)
from oddish.blocks.analyzer.claude_cli_client import (
    ClaudeCliClient,
    CliConfig,
    extract_claude_result,
    parse_cli_envelope,
    parse_stream_json_result,
)

_ENVELOPE = json.dumps(
    {
        "structured_output": {"classification": "GOOD_SUCCESS"},
        "total_cost_usd": 0.25,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
)


class _FakeProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode=0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self.hangs = False
        self.killed = False

    async def communicate(self):
        if self.hangs and not self.killed:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr

    def kill(self):
        self.killed = True


def _fake_exec(monkeypatch, process: _FakeProcess) -> dict:
    captured: dict = {}

    async def _exec(*command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(
        claude_cli_client.asyncio, "create_subprocess_exec", _exec
    )
    return captured


async def _drain(client, prompt="classify", **kw) -> str:
    return "".join([chunk async for chunk in client.stream(prompt, **kw)])


@pytest.mark.asyncio
async def test_create_llm_client_provisions_the_cli_backend(monkeypatch, tmp_path):
    """The point of the exercise: no caller-supplied factory."""
    _fake_exec(monkeypatch, _FakeProcess(stdout=_ENVELOPE.encode()))

    client = await create_llm_client(
        LLMClientType.CLAUDE_CLI,
        model="anthropic/claude-haiku-4-5",
        cli_config=CliConfig(cwd=tmp_path),
    )

    assert isinstance(client, ClaudeCliClient)
    assert await _drain(client) == _ENVELOPE


@pytest.mark.asyncio
async def test_create_llm_client_requires_a_cli_config(tmp_path):
    """Without a cwd there is no filesystem to bind to, and claude-code would
    silently read whatever directory the worker happens to sit in."""
    with pytest.raises(ValueError, match="cli_config"):
        await create_llm_client(LLMClientType.CLAUDE_CLI, model="anthropic/test")


@pytest.mark.asyncio
async def test_cli_client_binds_the_command_to_its_config(monkeypatch, tmp_path):
    task_dir = tmp_path / "task"
    qa_dir = tmp_path / "trial" / ".qa_context"
    captured = _fake_exec(monkeypatch, _FakeProcess(stdout=_ENVELOPE.encode()))

    client = ClaudeCliClient(
        model="anthropic/claude-haiku-4-5",
        config=CliConfig(
            cwd=tmp_path,
            add_dirs=(task_dir, qa_dir),
            json_schema='{"type":"object"}',
        ),
    )
    await _drain(client)

    command = captured["command"]
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert command[command.index("--json-schema") + 1] == '{"type":"object"}'
    assert command[command.index("--tools") + 1] == "Read,Glob"
    add_dirs = [command[i + 1] for i, a in enumerate(command) if a == "--add-dir"]
    assert add_dirs == [str(task_dir), str(qa_dir)]


@pytest.mark.asyncio
async def test_cli_client_forwards_a_system_prompt(monkeypatch, tmp_path):
    """The protocol carries system_prompt; dropping it silently is the same
    class of bug as the schema flag that only reached one backend."""
    captured = _fake_exec(monkeypatch, _FakeProcess(stdout=_ENVELOPE.encode()))

    client = ClaudeCliClient(model="anthropic/test", config=CliConfig(cwd=tmp_path))
    await _drain(client, system_prompt="be terse")

    command = captured["command"]
    assert command[command.index("--append-system-prompt") + 1] == "be terse"


@pytest.mark.asyncio
async def test_cli_client_records_usage_from_the_envelope(monkeypatch, tmp_path):
    _fake_exec(monkeypatch, _FakeProcess(stdout=_ENVELOPE.encode()))

    client = ClaudeCliClient(model="anthropic/test", config=CliConfig(cwd=tmp_path))
    await _drain(client)

    assert client.last_usage is not None
    assert client.last_usage.cost_usd == 0.25
    assert client.last_usage.input_tokens == 100
    assert client.last_usage.source == "native"


@pytest.mark.asyncio
async def test_cli_client_kills_the_process_on_timeout(monkeypatch, tmp_path):
    process = _FakeProcess(stdout=_ENVELOPE.encode())
    process.hangs = True
    _fake_exec(monkeypatch, process)

    client = ClaudeCliClient(
        model="anthropic/test", config=CliConfig(cwd=tmp_path, timeout=0.01)
    )

    with pytest.raises(TimeoutError):
        await _drain(client)
    assert process.killed is True


@pytest.mark.asyncio
async def test_cli_client_raises_with_stderr_on_a_nonzero_exit(monkeypatch, tmp_path):
    _fake_exec(
        monkeypatch, _FakeProcess(stderr=b"model not found", returncode=1)
    )

    client = ClaudeCliClient(model="anthropic/test", config=CliConfig(cwd=tmp_path))

    with pytest.raises(RuntimeError, match="model not found"):
        await _drain(client)


def test_extract_claude_result_prefers_structured_output():
    payload = {"structured_output": {"a": 1}, "result": "ignored"}
    assert extract_claude_result(payload) == {"a": 1}


def test_extract_claude_result_falls_back_to_a_result_dict():
    # claude-code emits an explicit null when schema validation produced
    # nothing, which must not be mistaken for "no result at all".
    payload = {"structured_output": None, "result": {"a": 1}}
    assert extract_claude_result(payload) == {"a": 1}


def test_extract_claude_result_parses_a_fenced_result_string():
    payload = {"result": '```json\n{"a": 1}\n```'}
    assert extract_claude_result(payload) == {"a": 1}


def test_extract_claude_result_rejects_an_empty_envelope():
    with pytest.raises(RuntimeError):
        extract_claude_result({"result": None})


def test_parse_cli_envelope_reads_one_json_object():
    assert parse_cli_envelope(_ENVELOPE) == {"classification": "GOOD_SUCCESS"}


def test_parse_stream_json_result_reads_the_last_result_event():
    raw = (
        '{"type": "assistant", "message": "thinking"}'
        '{"type": "result", "structured_output": {"classification": "GOOD_SUCCESS"}}'
    )
    assert parse_stream_json_result(raw) == {"classification": "GOOD_SUCCESS"}


def test_parse_stream_json_result_without_a_result_event_raises():
    with pytest.raises(RuntimeError, match="no structured result"):
        parse_stream_json_result('{"type": "assistant", "message": "hi"}')


def test_resolve_analysis_model_and_env_moved_with_the_subprocess(monkeypatch):
    """It exists to build this subprocess's env; leaving it on the classifier
    would make analyzer_llm_client import the classifier."""
    monkeypatch.setattr(
        claude_cli_client.settings, "claude_code_force_direct_api", True
    )
    model_id, env = claude_cli_client.resolve_analysis_model_and_env(
        "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        {"ANTHROPIC_API_KEY": "sk-test", "CLAUDE_CODE_USE_BEDROCK": "1"},
    )
    assert model_id == "claude-haiku-4-5"
    assert "CLAUDE_CODE_USE_BEDROCK" not in env


def test_cli_config_defaults_to_read_only_tools():
    config = CliConfig(cwd=Path("/tmp"))
    assert config.allowed_tools == ("Read", "Glob")
