import asyncio
import json

import pytest

from live_tail_fakes import (
    FakeEnv,
    FakeResult,
    b64,
    b64_raw,
    make_tailer,
    patch_db,
    update_params,
)
from oddish.workers.harbor import live_tail
from oddish.workers.harbor.live_tail import (
    ClaudeUsageFold,
    CodexUsageFold,
    CursorUsageFold,
    MiniSweUsageFold,
    UsageTotals,
    price_totals,
    split_lines,
)


def assistant_line(msg_id, usage, model="claude-opus-4-8"):
    return json.dumps(
        {"type": "assistant", "message": {"id": msg_id, "model": model, "usage": usage}}
    ).encode()


def test_fold_keeps_last_usage_per_message_id():
    fold = ClaudeUsageFold()
    fold.feed_line(assistant_line("msg_1", {"input_tokens": 10, "output_tokens": 1}))
    fold.feed_line(assistant_line("msg_1", {"input_tokens": 10, "output_tokens": 50}))
    fold.feed_line(
        assistant_line(
            "msg_2",
            {
                "input_tokens": 5,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "output_tokens": 7,
            },
        )
    )
    totals = fold.totals()
    assert totals.input_tokens == 10 + (5 + 100 + 20)
    assert totals.cache_tokens == 100
    assert totals.cache_write_tokens == 20
    assert totals.output_tokens == 50 + 7
    assert totals.model == "claude-opus-4-8"


def test_split_lines_carries_partial_tail():
    lines, rest = split_lines(b'{"a":1}\n{"b"')
    assert lines == [b'{"a":1}']
    assert rest == b'{"b"'
    lines, rest = split_lines(rest + b":2}\n")
    assert lines == [b'{"b":2}']
    assert rest == b""


def test_split_lines_survives_multibyte_split():
    euro_line = '{"text":"€"}\n'.encode()
    cut = 9
    first, second = euro_line[:cut], euro_line[cut:]
    assert first.count(b"\n") == 0
    lines, rest = split_lines(first)
    assert lines == [] and rest == first
    lines, rest = split_lines(rest + second)
    assert rest == b""
    assert json.loads(lines[0]) == {"text": "€"}


def test_price_totals_passes_bundled_input_by_keyword(monkeypatch):
    seen = {}

    def fake_estimate(model_name, **kwargs):
        seen["model"] = model_name
        seen.update(kwargs)
        return 1.23

    monkeypatch.setattr(live_tail, "estimate_cost_usd", fake_estimate)
    totals = UsageTotals(
        input_tokens=135,
        cache_tokens=100,
        cache_write_tokens=20,
        output_tokens=57,
        model="claude-opus-4-8",
    )
    assert price_totals(totals) == 1.23
    assert seen == {
        "model": "claude-opus-4-8",
        "input_tokens": 135,
        "output_tokens": 57,
        "cached_tokens": 100,
        "cache_write_tokens": 20,
    }


def test_price_totals_uses_seeded_fold_model(monkeypatch):
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda model, **_: model)
    seeded = ClaudeUsageFold(model="fallback-model")
    seeded.feed_line(assistant_line("m", {"input_tokens": 1}, model=""))
    assert price_totals(seeded.totals()) == "fallback-model"
    assert price_totals(ClaudeUsageFold().totals()) is None


def test_adapter_dispatch():
    assert live_tail.supports("claude-code")
    assert live_tail.supports("claude-code@2.1")
    assert live_tail.supports("Codex")
    assert live_tail.supports("codex-api-key-no-search")
    assert live_tail.supports("cursor-cli")
    assert live_tail.supports("cursor-cli-api-key-no-search")
    assert live_tail.supports("mini-swe-agent")
    assert not live_tail.supports("gemini-cli")
    assert not live_tail.supports("terminus-2")
    assert not live_tail.supports("")
    assert not live_tail.supports(None)
    claude = live_tail._adapter_for("glm-claude-code")
    assert claude.log_path == "/logs/agent/claude-code.txt"
    assert claude.snapshot is False
    assert isinstance(claude.make_fold(None), ClaudeUsageFold)
    codex = live_tail._adapter_for("codex")
    assert live_tail._adapter_for("codex-api-key-no-search") is codex
    assert codex.log_path == "/logs/agent/codex.txt"
    assert codex.make_fold("m").model == "m"
    assert isinstance(codex.make_fold("m"), CodexUsageFold)
    cursor = live_tail._adapter_for("cursor-cli")
    assert live_tail._adapter_for("cursor-cli-api-key-no-search") is cursor
    assert cursor.log_path == "/logs/agent/cursor-cli.txt"
    assert cursor.snapshot is False
    assert isinstance(cursor.make_fold("m"), CursorUsageFold)
    mini = live_tail._adapter_for("mini-swe-agent")
    assert mini.log_path == "/logs/agent/mini-swe-agent.trajectory.json"
    assert mini.snapshot is True
    assert isinstance(mini.make_fold("m"), MiniSweUsageFold)


def codex_line(obj) -> bytes:
    return json.dumps(obj).encode()


def codex_item(item) -> bytes:
    return codex_line({"type": "item.completed", "item": item})


def test_codex_fold_keeps_last_cumulative_usage():
    fold = CodexUsageFold(model="gpt-5.3-codex")
    assert not fold.has_usage
    assert (
        fold.feed_line(
            codex_line(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 40,
                        "output_tokens": 10,
                    },
                }
            )
        )
        == []
    )
    assert (
        fold.feed_line(
            codex_line(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 250,
                        "cached_input_tokens": 90,
                        "output_tokens": 30,
                    },
                }
            )
        )
        == []
    )
    assert fold.has_usage
    totals = fold.totals()
    assert totals.input_tokens == 250
    assert totals.cache_tokens == 90
    assert totals.cache_write_tokens == 0
    assert totals.output_tokens == 30
    assert totals.model == "gpt-5.3-codex"


def test_codex_fold_banks_usage_across_truncation():
    fold = CodexUsageFold(model="gpt-5.3-codex")
    fold.feed_line(
        codex_line(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 40,
                    "output_tokens": 10,
                },
            }
        )
    )
    fold.on_truncate()
    assert fold.has_usage
    assert fold.usage is None
    assert fold.totals().input_tokens == 100
    fold.feed_line(
        codex_line(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 30,
                    "cached_input_tokens": 5,
                    "output_tokens": 4,
                },
            }
        )
    )
    totals = fold.totals()
    assert totals.input_tokens == 130
    assert totals.cache_tokens == 45
    assert totals.output_tokens == 14
    assert totals.model == "gpt-5.3-codex"


def test_codex_fold_renders_display_kinds():
    fold = CodexUsageFold()
    assert fold.feed_line(codex_item({"type": "agent_message", "text": "hi"})) == [
        {"kind": "message", "payload": {"text": "hi"}}
    ]
    assert fold.feed_line(codex_item({"type": "reasoning", "text": "thinking"})) == [
        {"kind": "message", "payload": {"text": "thinking"}}
    ]
    rendered = fold.feed_line(
        codex_item(
            {
                "type": "command_execution",
                "command": "ls",
                "aggregated_output": "out",
                "exit_code": 1,
            }
        )
    )
    assert rendered == [
        {
            "kind": "tool_use",
            "payload": {"input": json.dumps({"command": "ls"}), "name": "shell"},
        },
        {"kind": "tool_result", "payload": {"content": "out", "is_error": True}},
    ]
    [_, tool_result] = fold.feed_line(
        codex_item({"type": "command_execution", "command": "pwd", "exit_code": 0})
    )
    assert tool_result["payload"] == {"content": ""}
    assert fold.feed_line(
        codex_line({"type": "turn.failed", "error": {"message": "boom"}})
    ) == [{"kind": "summary", "payload": {"text": "boom"}}]


def test_codex_fold_ignores_garbage_and_unknown_events():
    fold = CodexUsageFold()
    garbage = [
        b"",
        b"[stderr] npm warn deprecated",
        codex_line({"type": "thread.started", "thread_id": "th_1"}),
        codex_line({"type": "item.started", "item": {"type": "command_execution"}}),
        codex_item({"type": "todo_list"}),
        codex_item({"type": "agent_message"}),
        codex_line({"type": "item.completed"}),
        codex_line({"type": "turn.completed", "usage": 5}),
        codex_line({"type": "turn.failed"}),
        codex_line({"type": "turn.failed", "error": "nope"}),
    ]
    for raw in garbage:
        assert fold.feed_line(raw) == []
    assert not fold.has_usage
    assert fold.totals() == UsageTotals()


@pytest.mark.asyncio
async def test_codex_tick_tails_codex_log_and_checkpoints(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: 0.25)
    raw = (
        codex_item({"type": "agent_message", "text": "hi"})
        + b"\n"
        + codex_line(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 7,
                    "cached_input_tokens": 2,
                    "output_tokens": 3,
                },
            }
        )
        + b"\n"
    )
    env = FakeEnv([b64(raw)])
    tailer = make_tailer(env, agent="codex", model="gpt-5.3-codex")
    await tailer._tick()
    assert "'/logs/agent/codex.txt'" in env.commands[0]
    params = update_params(session)
    assert params[-1]["input_tokens"] == 7
    assert params[-1]["cache_tokens"] == 2
    assert params[-1]["cache_write_tokens"] == 0
    assert params[-1]["output_tokens"] == 3
    assert params[-1]["cost_usd"] == 0.25


@pytest.mark.asyncio
async def test_codex_tick_without_usage_skips_checkpoint(monkeypatch):
    session = patch_db(monkeypatch)
    env = FakeEnv([b64(codex_item({"type": "agent_message", "text": "hi"}) + b"\n")])
    tailer = make_tailer(env, agent="codex")
    await tailer._tick()
    assert update_params(session) == []


@pytest.mark.asyncio
async def test_tick_offset_math_and_checkpoint_across_split_chunks(monkeypatch):
    session = patch_db(monkeypatch, price=0.5)
    line1 = assistant_line("msg_1", {"input_tokens": 10, "output_tokens": 2})
    line2 = assistant_line("msg_2", {"input_tokens": 3, "output_tokens": 4})
    raw1 = line1 + b"\n" + line2[:7]
    raw2 = line2[7:] + b"\n"
    env = FakeEnv([b64(raw1), b64(raw2), FakeResult()])
    tailer = make_tailer(env)

    await tailer._tick()
    assert tailer.offset == len(raw1)
    assert tailer.carry == line2[:7]
    assert set(tailer.fold.usage_by_id) == {"msg_1"}

    await tailer._tick()
    assert tailer.offset == len(raw1) + len(raw2)
    assert tailer.carry == b""
    assert set(tailer.fold.usage_by_id) == {"msg_1", "msg_2"}

    await tailer._tick()
    params = update_params(session)
    assert len(params) == 2
    assert params[0]["input_tokens"] == 10 and params[0]["output_tokens"] == 2
    assert params[1]["input_tokens"] == 13 and params[1]["output_tokens"] == 6
    assert params[1]["cost_usd"] == 0.5
    assert "+1 " in env.commands[0] and f"+{len(raw1) + 1} " in env.commands[1]


@pytest.mark.asyncio
async def test_checkpoint_writes_null_cost_when_unpriceable(monkeypatch):
    session = patch_db(monkeypatch)
    env = FakeEnv([b64(assistant_line("m", {"input_tokens": 1}) + b"\n")])
    tailer = make_tailer(env, model="m")
    await tailer._tick()
    params = update_params(session)
    assert params[-1]["cost_usd"] is None


@pytest.mark.asyncio
async def test_tick_resets_offset_when_log_truncated(monkeypatch):
    patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    line1 = assistant_line("m1", {"input_tokens": 1}) + b"\n"
    retry = assistant_line("m2", {"input_tokens": 9}) + b"\n"
    env = FakeEnv(
        [
            b64(line1, size=len(line1)),
            b64(b"", size=3),
            b64(retry, size=len(retry)),
        ]
    )
    tailer = make_tailer(env)
    await tailer._tick()
    assert tailer.offset == len(line1)
    tailer.carry = b"torn"
    await tailer._tick()
    assert tailer.offset == 0
    assert tailer.carry == b""
    await tailer._tick()
    assert "tail -c +1 '" in env.commands[2]
    assert tailer.offset == len(retry)
    assert set(tailer.fold.usage_by_id) == {"m1", "m2"}


@pytest.mark.asyncio
async def test_codex_tick_banks_usage_when_log_truncates(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)

    def turn(inp, out):
        return (
            codex_line(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": inp,
                        "cached_input_tokens": 0,
                        "output_tokens": out,
                    },
                }
            )
            + b"\n"
        )

    turn1, turn2 = turn(100, 10), turn(30, 4)
    env = FakeEnv(
        [
            b64(turn1, size=len(turn1)),
            b64(b"", size=3),
            b64(turn2, size=len(turn2)),
        ]
    )
    tailer = make_tailer(env, agent="codex", model="gpt-5.3-codex")
    await tailer._tick()
    assert update_params(session)[-1]["input_tokens"] == 100
    await tailer._tick()
    assert tailer.offset == 0
    await tailer._tick()
    assert "tail -c +1 '" in env.commands[2]
    params = update_params(session)
    assert params[-1]["input_tokens"] == 130
    assert params[-1]["output_tokens"] == 14


def cursor_line(obj) -> bytes:
    return json.dumps(obj).encode()


def test_cursor_fold_renders_display_kinds():
    fold = CursorUsageFold()
    assert fold.feed_line(
        cursor_line(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hello"}]},
            }
        )
    ) == [{"kind": "message", "payload": {"text": "hello"}}]
    assert fold.feed_line(
        cursor_line({"type": "thinking", "subtype": "completed", "text": "pondering"})
    ) == [{"kind": "message", "payload": {"text": "pondering"}}]
    assert (
        fold.feed_line(
            cursor_line({"type": "thinking", "subtype": "delta", "text": "part"})
        )
        == []
    )
    rendered = fold.feed_line(
        cursor_line(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": "c1",
                "tool_call": {
                    "shell": {"args": {"command": "ls"}, "result": "out"},
                },
            }
        )
    )
    assert rendered == [
        {
            "kind": "tool_use",
            "payload": {"input": json.dumps({"command": "ls"}), "name": "shell"},
        },
        {"kind": "tool_result", "payload": {"content": "out"}},
    ]
    assert (
        fold.feed_line(
            cursor_line(
                {
                    "type": "tool_call",
                    "subtype": "started",
                    "call_id": "c1",
                    "tool_call": {"shell": {"args": {"command": "ls"}}},
                }
            )
        )
        == []
    )
    assert fold.feed_line(
        cursor_line({"type": "result", "subtype": "success", "result": "done"})
    ) == [{"kind": "summary", "payload": {"text": "done"}}]


def test_cursor_fold_sums_result_usage():
    fold = CursorUsageFold(model="composer-2.5")
    assert not fold.has_usage

    def result(inp, cread, cwrite, out):
        return cursor_line(
            {
                "type": "result",
                "subtype": "success",
                "result": "",
                "usage": {
                    "inputTokens": inp,
                    "cacheReadTokens": cread,
                    "cacheWriteTokens": cwrite,
                    "outputTokens": out,
                },
            }
        )

    assert fold.feed_line(result(10, 5, 2, 3)) == []
    assert fold.feed_line(result(20, 4, 1, 6)) == []
    assert fold.has_usage
    totals = fold.totals()
    assert totals.input_tokens == (10 + 20) + (5 + 4) + (2 + 1)
    assert totals.cache_tokens == 5 + 4
    assert totals.cache_write_tokens == 2 + 1
    assert totals.output_tokens == 3 + 6
    assert totals.model == "composer-2.5"


def test_cursor_fold_ignores_garbage():
    fold = CursorUsageFold()
    for raw in (
        b"",
        b"[warn] booting cursor-agent",
        cursor_line({"type": "system", "subtype": "init", "model": "composer-2.5"}),
        cursor_line({"type": "interaction_query", "session_id": "s"}),
        cursor_line({"type": "assistant", "message": {"content": []}}),
        cursor_line({"type": "tool_call", "subtype": "completed"}),
        cursor_line({"type": "result", "subtype": "success", "result": "", "usage": 5}),
    ):
        assert fold.feed_line(raw) == []
    assert not fold.has_usage
    assert fold.totals() == UsageTotals()


@pytest.mark.asyncio
async def test_cursor_tick_tails_cursor_log_and_checkpoints(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: 0.75)
    raw = (
        cursor_line(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "hi"}]},
            }
        )
        + b"\n"
        + cursor_line(
            {
                "type": "result",
                "subtype": "success",
                "result": "done",
                "usage": {
                    "inputTokens": 7,
                    "cacheReadTokens": 2,
                    "cacheWriteTokens": 1,
                    "outputTokens": 3,
                },
            }
        )
        + b"\n"
    )
    env = FakeEnv([b64(raw)])
    tailer = make_tailer(env, agent="cursor-cli", model="composer-2.5")
    await tailer._tick()
    assert "'/logs/agent/cursor-cli.txt'" in env.commands[0]
    params = update_params(session)
    assert params[-1]["input_tokens"] == 10
    assert params[-1]["cache_tokens"] == 2
    assert params[-1]["cache_write_tokens"] == 1
    assert params[-1]["output_tokens"] == 3
    assert params[-1]["cost_usd"] == 0.75


def mini_trajectory(messages, model="anthropic/claude-opus-4-8", cost=None):
    info = {"config": {"model": {"model_name": model}}}
    if cost is not None:
        info["model_stats"] = {"instance_cost": cost}
    return json.dumps({"info": info, "messages": messages}).encode()


def mini_assistant(text, command=None, usage=None):
    message = {"role": "assistant", "content": text}
    if command is not None:
        message["tool_calls"] = [
            {"function": {"name": "bash", "arguments": {"command": command}}}
        ]
    if usage is not None:
        message["extra"] = {"response": {"usage": usage}}
    return message


def test_mini_swe_fold_emits_new_messages_only():
    fold = MiniSweUsageFold()
    first = fold.feed_line(
        mini_trajectory(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "solve it"},
                mini_assistant("let me look", command="ls"),
            ]
        )
    )
    assert first == [
        {"kind": "message", "payload": {"text": "let me look"}},
        {
            "kind": "tool_use",
            "payload": {"input": json.dumps({"command": "ls"}), "name": "bash"},
        },
    ]
    assert (
        fold.feed_line(
            mini_trajectory(
                [
                    {"role": "system", "content": "sys"},
                    {"role": "user", "content": "solve it"},
                    mini_assistant("let me look", command="ls"),
                ]
            )
        )
        == []
    )
    grown = fold.feed_line(
        mini_trajectory(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "solve it"},
                mini_assistant("let me look", command="ls"),
                {"role": "tool", "content": "file.txt"},
                mini_assistant("done"),
            ]
        )
    )
    assert grown == [
        {"kind": "tool_result", "payload": {"content": "file.txt"}},
        {"kind": "message", "payload": {"text": "done"}},
    ]


def test_mini_swe_fold_accumulates_usage_from_snapshot():
    fold = MiniSweUsageFold(model="fallback")
    assert not fold.has_usage
    fold.feed_line(
        mini_trajectory(
            [
                {"role": "user", "content": "task"},
                mini_assistant(
                    "a",
                    usage={
                        "prompt_tokens": 100,
                        "completion_tokens": 10,
                        "prompt_tokens_details": {"cached_tokens": 40},
                    },
                ),
                mini_assistant(
                    "b",
                    usage={
                        "prompt_tokens": 50,
                        "completion_tokens": 5,
                        "prompt_tokens_details": {"cached_tokens": 20},
                    },
                ),
            ],
            model="anthropic/claude-opus-4-8",
        )
    )
    assert fold.has_usage
    totals = fold.totals()
    assert totals.input_tokens == 150
    assert totals.cache_tokens == 60
    assert totals.output_tokens == 15
    assert totals.model == "anthropic/claude-opus-4-8"


def test_mini_swe_fold_ignores_garbage():
    fold = MiniSweUsageFold()
    for raw in (b"", b"not json", b"{truncated", json.dumps([1, 2]).encode()):
        assert fold.feed_line(raw) == []
    assert not fold.has_usage
    assert fold.totals() == UsageTotals()


@pytest.mark.asyncio
async def test_mini_swe_snapshot_tick_reads_whole_file(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: 0.4)
    snapshot = mini_trajectory(
        [
            {"role": "user", "content": "task"},
            mini_assistant(
                "working",
                command="ls",
                usage={
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 3},
                },
            ),
        ],
        model="anthropic/claude-opus-4-8",
    )
    env = FakeEnv([b64_raw(snapshot)])
    tailer = make_tailer(env, agent="mini-swe-agent", model="anthropic/claude-opus-4-8")
    await tailer._tick()
    command = env.commands[0]
    assert "'/logs/agent/mini-swe-agent.trajectory.json'" in command
    assert f"head -c {live_tail.SNAPSHOT_MAX_BYTES}" in command
    assert "set -o pipefail" not in command
    assert tailer.offset == 0
    params = update_params(session)
    assert params[-1]["input_tokens"] == 12
    assert params[-1]["cache_tokens"] == 3
    assert params[-1]["output_tokens"] == 4
    assert params[-1]["cost_usd"] == 0.4


@pytest.mark.asyncio
async def test_mini_swe_snapshot_tick_incremental_across_ticks(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    msgs = [{"role": "user", "content": "task"}, mini_assistant("first")]
    grown = msgs + [mini_assistant("second")]
    env = FakeEnv([b64_raw(mini_trajectory(msgs)), b64_raw(mini_trajectory(grown))])
    tailer = make_tailer(env, agent="mini-swe-agent")
    await tailer._tick()
    await tailer._tick()
    from test_trial_events import insert_rows, insert_stmts

    rows = [r for s in insert_stmts(session) for r in insert_rows(s)]
    assert [(r["seq"], r["kind"], r["payload"]["text"]) for r in rows] == [
        (1, "message", "first"),
        (2, "message", "second"),
    ]


@pytest.mark.asyncio
async def test_snapshot_tick_raises_on_invalid_base64():
    env = FakeEnv([FakeResult(stdout="not-base64!!")])
    tailer = make_tailer(env, agent="mini-swe-agent")
    with pytest.raises(RuntimeError):
        await tailer._tick()


@pytest.mark.asyncio
async def test_snapshot_tick_raises_on_nonzero_rc():
    env = FakeEnv([FakeResult(return_code=1)])
    tailer = make_tailer(env, agent="mini-swe-agent")
    with pytest.raises(RuntimeError):
        await tailer._tick()


@pytest.mark.asyncio
async def test_snapshot_tick_tolerates_empty_output(monkeypatch):
    session = patch_db(monkeypatch)
    env = FakeEnv([FakeResult(stdout="")])
    tailer = make_tailer(env, agent="mini-swe-agent")
    await tailer._tick()
    assert update_params(session) == []


def test_cursor_completed_tool_call_without_result_emits_empty():
    fold = CursorUsageFold()
    assert fold.feed_line(
        cursor_line(
            {
                "type": "tool_call",
                "subtype": "completed",
                "call_id": "c1",
                "tool_call": {"shell": {"args": {"command": "true"}, "result": None}},
            }
        )
    ) == [
        {
            "kind": "tool_use",
            "payload": {"input": json.dumps({"command": "true"}), "name": "shell"},
        },
        {"kind": "tool_result", "payload": {"content": ""}},
    ]


def test_cursor_fold_survives_non_numeric_usage():
    fold = CursorUsageFold(model="composer-2.5")
    fold.feed_line(
        cursor_line(
            {
                "type": "result",
                "subtype": "success",
                "result": "",
                "usage": {"inputTokens": "bad", "outputTokens": None},
            }
        )
    )
    assert fold.has_usage
    assert fold.totals() == UsageTotals(model="composer-2.5")


def test_cursor_on_truncate_resets_summed_usage():
    fold = CursorUsageFold(model="composer-2.5")

    def result(inp, out):
        return cursor_line(
            {
                "type": "result",
                "subtype": "success",
                "result": "",
                "usage": {"inputTokens": inp, "outputTokens": out},
            }
        )

    fold.feed_line(result(10, 3))
    fold.on_truncate()
    assert not fold.has_usage
    assert fold.totals() == UsageTotals(model="composer-2.5")
    fold.feed_line(result(10, 3))
    assert fold.totals().input_tokens == 10
    assert fold.totals().output_tokens == 3


def test_mini_swe_fold_totals_are_monotonic_on_shrink():
    fold = MiniSweUsageFold()
    fold.feed_line(
        mini_trajectory(
            [
                {"role": "user", "content": "task"},
                mini_assistant("a", usage={"prompt_tokens": 5, "completion_tokens": 1}),
                mini_assistant("b", usage={"prompt_tokens": 5, "completion_tokens": 1}),
            ]
        )
    )
    high_water = fold.totals()
    shrunk = fold.feed_line(mini_trajectory([{"role": "user", "content": "new"}]))
    assert shrunk == []
    assert fold.has_usage
    assert fold.totals() == high_water


def test_mini_swe_fold_renders_later_user_as_observation():
    fold = MiniSweUsageFold()
    rendered = fold.feed_line(
        mini_trajectory(
            [
                {"role": "user", "content": "task"},
                mini_assistant("look"),
                {"role": "user", "content": "observation"},
            ]
        )
    )
    assert rendered == [
        {"kind": "message", "payload": {"text": "look"}},
        {"kind": "tool_result", "payload": {"content": "observation"}},
    ]


def test_mini_swe_fold_survives_malformed_messages():
    fold = MiniSweUsageFold()
    rendered = fold.feed_line(
        mini_trajectory(
            [
                {"role": "user", "content": "task"},
                {"role": "assistant", "content": "x", "extra": "bad"},
                {"role": "assistant", "tool_calls": [{"function": "bad"}]},
            ]
        )
    )
    assert {e["kind"] for e in rendered} <= {"message", "tool_use"}
    assert not fold.has_usage


@pytest.mark.asyncio
async def test_tick_rc_semantics():
    env = FakeEnv([FakeResult(return_code=1)])
    tailer = make_tailer(env)
    await tailer._tick()

    env = FakeEnv([FakeResult(return_code=127)])
    tailer = make_tailer(env)
    with pytest.raises(RuntimeError):
        await tailer._tick()

    env = FakeEnv([FakeResult(return_code=1)])
    tailer = make_tailer(env)
    tailer.offset = 10
    with pytest.raises(RuntimeError):
        await tailer._tick()


@pytest.mark.asyncio
async def test_run_disables_after_consecutive_failures(monkeypatch):
    monkeypatch.setattr(live_tail.settings, "live_tail_interval_sec", 0.001)
    env = FakeEnv([RuntimeError("boom")] * 10)
    tailer = make_tailer(env)
    await asyncio.wait_for(tailer.run(), timeout=5)
    assert len(env.commands) == live_tail.MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_failure_cap_persists_pending_fold(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail.settings, "live_tail_interval_sec", 0.001)
    env = FakeEnv([RuntimeError("boom")] * 10)
    tailer = make_tailer(env)
    tailer.fold.feed_line(assistant_line("m", {"input_tokens": 2}))
    await asyncio.wait_for(tailer.run(), timeout=5)
    assert len(env.commands) == live_tail.MAX_CONSECUTIVE_FAILURES
    assert update_params(session)[-1]["input_tokens"] == 2


@pytest.mark.asyncio
async def test_stop_triggers_final_drain(monkeypatch):
    session = patch_db(monkeypatch)
    env = FakeEnv([b64(assistant_line("m", {"input_tokens": 5}) + b"\n")])
    tailer = make_tailer(env)
    tailer.request_stop()
    await asyncio.wait_for(tailer.run(), timeout=5)
    assert len(env.commands) == 1
    assert update_params(session)[-1]["input_tokens"] == 5


@pytest.mark.asyncio
async def test_cost_is_monotone_across_unpriceable_ticks(monkeypatch):
    session = patch_db(monkeypatch)
    prices = iter([0.5, None, 0.4, 0.9])
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: next(prices))
    chunks = [
        b64(assistant_line(f"m{i}", {"input_tokens": i + 1}) + b"\n") for i in range(4)
    ]
    env = FakeEnv(chunks)
    tailer = make_tailer(env, model="m")
    for _ in range(4):
        await tailer._tick()
    costs = [p["cost_usd"] for p in update_params(session)]
    assert costs == [0.5, 0.5, 0.5, 0.9]


@pytest.mark.asyncio
async def test_stop_path_persists_fold_when_final_tick_fails(monkeypatch):
    session = patch_db(monkeypatch)
    env = FakeEnv([RuntimeError("sandbox died")])
    tailer = make_tailer(env)
    tailer.fold.feed_line(assistant_line("m", {"input_tokens": 6}))
    tailer.request_stop()
    await asyncio.wait_for(tailer.run(), timeout=5)
    assert update_params(session)[-1]["input_tokens"] == 6


@pytest.mark.asyncio
async def test_cancelled_tailer_persists_fold_unless_replaced(monkeypatch):
    session = patch_db(monkeypatch)

    class HangingEnv:
        async def exec(self, command, timeout_sec=None):
            await asyncio.Event().wait()

    for replaced, expected_writes in ((False, 1), (True, 0)):
        session.stmts.clear()
        tailer = make_tailer(HangingEnv())
        tailer.fold.feed_line(assistant_line("m", {"input_tokens": 4}))
        tailer.replaced = replaced
        task = asyncio.ensure_future(tailer.run())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(session.stmts) == expected_writes


@pytest.mark.asyncio
async def test_empty_tick_retries_pending_checkpoint(monkeypatch):
    session = patch_db(monkeypatch)
    env = FakeEnv([FakeResult()])
    tailer = make_tailer(env)
    tailer.fold.feed_line(assistant_line("m", {"input_tokens": 3}))
    await tailer._tick()
    assert update_params(session)[-1]["input_tokens"] == 3


@pytest.mark.asyncio
async def test_invalid_base64_raises_exec_error():
    env = FakeEnv([FakeResult(stdout="not-base64!!")])
    tailer = make_tailer(env)
    with pytest.raises(RuntimeError):
        await tailer._tick()


@pytest.mark.asyncio
async def test_zero_rowcount_stops_tailer(monkeypatch):
    patch_db(monkeypatch, rowcount=0)
    env = FakeEnv([b64(assistant_line("m", {"input_tokens": 1}) + b"\n")])
    tailer = make_tailer(env)
    await tailer._tick()
    assert tailer._stop.is_set()
    assert tailer._last_written is None


@pytest.mark.asyncio
async def test_replaced_tailer_skips_checkpoint(monkeypatch):
    session = patch_db(monkeypatch)
    env = FakeEnv([b64(assistant_line("m", {"input_tokens": 1}) + b"\n")])
    tailer = make_tailer(env)
    tailer.replaced = True
    await tailer._tick()
    assert session.stmts == []
