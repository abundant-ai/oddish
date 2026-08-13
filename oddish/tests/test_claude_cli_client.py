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
    parse_stream_json_result,
)

_ENVELOPE = json.dumps(
    {
        "type": "result",
        "structured_output": {"classification": "GOOD_SUCCESS"},
        "total_cost_usd": 0.25,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
)


class _FakeStdout:
    """One stream-json event per line; optionally hangs at EOF."""

    def __init__(self, data: bytes, process: "_FakeProcess") -> None:
        self._lines = [line + b"\n" for line in data.splitlines()]
        self._process = process

    async def readline(self) -> bytes:
        if self._lines:
            return self._lines.pop(0)
        if self._process.hangs and not self._process.killed:
            await asyncio.sleep(3600)
        return b""


class _FakeStderr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _FakeProcess:
    def __init__(self, *, stdout: bytes = b"", stderr: bytes = b"", returncode=0):
        self.stdout = _FakeStdout(stdout, self)
        self.stderr = _FakeStderr(stderr)
        self._exit_code = returncode
        self.returncode: int | None = None
        self.hangs = False
        self.killed = False

    async def wait(self) -> int:
        self.returncode = -9 if self.killed else self._exit_code
        return self.returncode

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
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in command
    assert command[command.index("--json-schema") + 1] == '{"type":"object"}'
    assert command[command.index("--tools") + 1] == "Read,Glob,Grep"
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
async def test_cli_client_yields_one_chunk_per_event_line(monkeypatch, tmp_path):
    """The analysis log renders each event as it arrives, so the client must
    yield per line instead of one blob at the end."""
    assistant = json.dumps({"type": "assistant", "message": {"content": []}})
    stdout = (assistant + "\n" + _ENVELOPE + "\n").encode()
    _fake_exec(monkeypatch, _FakeProcess(stdout=stdout))

    client = ClaudeCliClient(model="anthropic/test", config=CliConfig(cwd=tmp_path))
    chunks = [chunk async for chunk in client.stream("classify")]

    assert chunks == [assistant, _ENVELOPE]
    assert parse_stream_json_result("".join(chunks)) == {
        "classification": "GOOD_SUCCESS"
    }


@pytest.mark.asyncio
async def test_cli_client_does_not_yield_non_json_lines(monkeypatch, tmp_path):
    """Chunks are joined and parsed as adjacent JSON downstream, so one noise
    line on stdout must not reach the yielded stream."""
    stdout = (b"claude-code starting up\n" + _ENVELOPE.encode() + b"\n")
    _fake_exec(monkeypatch, _FakeProcess(stdout=stdout))

    client = ClaudeCliClient(model="anthropic/test", config=CliConfig(cwd=tmp_path))
    chunks = [chunk async for chunk in client.stream("classify")]

    assert chunks == [_ENVELOPE]


@pytest.mark.asyncio
async def test_cli_client_keeps_a_non_json_line_for_error_context(
    monkeypatch, tmp_path
):
    _fake_exec(
        monkeypatch,
        _FakeProcess(stdout=b"segfault in tool host\n", returncode=1),
    )

    client = ClaudeCliClient(model="anthropic/test", config=CliConfig(cwd=tmp_path))

    with pytest.raises(RuntimeError, match="segfault in tool host"):
        await _drain(client)


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


@pytest.mark.asyncio
async def test_cli_client_surfaces_the_result_message_not_the_envelope(
    monkeypatch, tmp_path
):
    """A failed run whose last stdout event is the CLI's JSON result envelope
    must raise only its human-readable `result` text — the full envelope
    (usage counters, session ids) renders as an unreadable blob wherever the
    error is shown, e.g. the Findings audit panel."""
    envelope = json.dumps(
        {
            "type": "result",
            "is_error": True,
            "session_id": "497be066-5c5f-4118-9c62-01a66c80aa07",
            "usage": {"input_tokens": 0, "output_tokens": 0},
            "result": "API Error: 400 You have reached your specified API usage limits.",
        }
    )
    _fake_exec(
        monkeypatch, _FakeProcess(stdout=f"{envelope}\n".encode(), returncode=1)
    )

    client = ClaudeCliClient(model="anthropic/test", config=CliConfig(cwd=tmp_path))

    with pytest.raises(RuntimeError) as excinfo:
        await _drain(client)
    message = str(excinfo.value)
    assert "API Error: 400 You have reached your specified API usage limits." in message
    assert "session_id" not in message
    assert "input_tokens" not in message


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


def test_parse_stream_json_result_reads_the_last_result_event():
    raw = (
        '{"type": "assistant", "message": "thinking"}'
        '{"type": "result", "structured_output": {"classification": "GOOD_SUCCESS"}}'
    )
    assert parse_stream_json_result(raw) == {"classification": "GOOD_SUCCESS"}


def test_parse_stream_json_result_skips_a_trailing_empty_result_event():
    raw = (
        '{"type": "result", "structured_output": {"classification": "GOOD_SUCCESS"}}'
        '{"type": "result", "structured_output": null, "result": null}'
    )
    assert parse_stream_json_result(raw) == {"classification": "GOOD_SUCCESS"}


def test_parse_stream_json_result_skips_non_json_noise_between_events():
    raw = (
        "claude-code starting up"
        '{"type": "assistant", "message": "thinking"}'
        "\x1b[32msome ansi banner\x1b[0m"
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
    assert config.allowed_tools == ("Read", "Glob", "Grep")
