import asyncio
import base64
import contextlib
import json

import pytest

from oddish.workers.harbor import live_tail
from oddish.workers.harbor.live_tail import (
    ClaudeUsageFold,
    LiveTailer,
    TailExecError,
    UsageTotals,
    price_totals,
    split_lines,
)


class FakeResult:
    def __init__(self, stdout="", return_code=0):
        self.stdout = stdout
        self.return_code = return_code


class FakeEnv:
    def __init__(self, results):
        self.results = list(results)
        self.commands = []

    async def exec(self, command, timeout_sec=None):
        self.commands.append(command)
        result = self.results.pop(0) if self.results else FakeResult()
        if isinstance(result, Exception):
            raise result
        return result


def b64(raw: bytes) -> FakeResult:
    return FakeResult(stdout=base64.b64encode(raw).decode())


class FakeExecuteResult:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount


class FakeSession:
    def __init__(self, rowcount=1):
        self.stmts = []
        self.rowcount = rowcount

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return FakeExecuteResult(self.rowcount)


def patch_db(monkeypatch, rowcount=1):
    session = FakeSession(rowcount)

    @contextlib.asynccontextmanager
    async def fake_get_session():
        yield session

    monkeypatch.setattr(live_tail, "get_session", fake_get_session)
    return session


def checkpoint_params(session):
    return [dict(stmt.compile().params) for stmt in session.stmts]


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


def test_fold_ignores_non_assistant_and_garbage():
    fold = ClaudeUsageFold()
    fold.feed_line(b"")
    fold.feed_line(b"not json {")
    fold.feed_line(b'{"type": "user", "message": {"id": "msg_x", "usage": {"input_tokens": 9}}}')
    fold.feed_line(b'{"type": "assistant", "message": {"id": "msg_y"}}')
    fold.feed_line(b'{"type": "assistant"}')
    fold.feed_line(b'[1, 2]')
    assert fold.usage_by_id == {}
    assert fold.totals() == UsageTotals()


def test_split_lines_carries_partial_tail():
    lines, rest = split_lines(b'{"a":1}\n{"b"')
    assert lines == [b'{"a":1}']
    assert rest == b'{"b"'
    lines, rest = split_lines(rest + b':2}\n')
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
    assert price_totals(totals, None) == 1.23
    assert seen == {
        "model": "claude-opus-4-8",
        "input_tokens": 135,
        "output_tokens": 57,
        "cached_tokens": 100,
        "cache_write_tokens": 20,
    }


def test_price_totals_falls_back_to_trial_model(monkeypatch):
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda model, **_: model)
    totals = UsageTotals(input_tokens=1)
    assert price_totals(totals, "fallback-model") == "fallback-model"
    assert price_totals(totals, None) is None


def test_supports_only_claude_code():
    assert live_tail.supports("claude-code")
    assert live_tail.supports("claude-code@2.1")
    assert not live_tail.supports("codex")
    assert not live_tail.supports(None)


@pytest.mark.asyncio
async def test_tick_offset_math_and_checkpoint_across_split_chunks(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: 0.5)
    line1 = assistant_line("msg_1", {"input_tokens": 10, "output_tokens": 2})
    line2 = assistant_line("msg_2", {"input_tokens": 3, "output_tokens": 4})
    raw1 = line1 + b"\n" + line2[:7]
    raw2 = line2[7:] + b"\n"
    env = FakeEnv([b64(raw1), b64(raw2), FakeResult()])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)

    await tailer._tick()
    assert tailer.offset == len(raw1)
    assert tailer.carry == line2[:7]
    assert set(tailer.fold.usage_by_id) == {"msg_1"}

    await tailer._tick()
    assert tailer.offset == len(raw1) + len(raw2)
    assert tailer.carry == b""
    assert set(tailer.fold.usage_by_id) == {"msg_1", "msg_2"}

    await tailer._tick()
    params = checkpoint_params(session)
    assert len(params) == 2
    assert params[0]["input_tokens"] == 10 and params[0]["output_tokens"] == 2
    assert params[1]["input_tokens"] == 13 and params[1]["output_tokens"] == 6
    assert params[1]["cost_usd"] == 0.5
    assert "+1 " in env.commands[0] and f"+{len(raw1) + 1} " in env.commands[1]


@pytest.mark.asyncio
async def test_checkpoint_writes_null_cost_when_unpriceable(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    env = FakeEnv([b64(assistant_line("m", {"input_tokens": 1}) + b"\n")])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model="m")
    await tailer._tick()
    params = checkpoint_params(session)
    assert params[-1]["cost_usd"] is None


@pytest.mark.asyncio
async def test_tick_rc_semantics():
    env = FakeEnv([FakeResult(return_code=1)])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    await tailer._tick()

    env = FakeEnv([FakeResult(return_code=127)])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    with pytest.raises(TailExecError):
        await tailer._tick()

    env = FakeEnv([FakeResult(return_code=1)])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    tailer.offset = 10
    with pytest.raises(TailExecError):
        await tailer._tick()


@pytest.mark.asyncio
async def test_run_disables_after_consecutive_failures(monkeypatch):
    monkeypatch.setattr(live_tail.settings, "live_tail_interval_sec", 0.001)
    env = FakeEnv([RuntimeError("boom")] * 10)
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    await asyncio.wait_for(tailer.run(), timeout=5)
    assert len(env.commands) == live_tail.MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_stop_triggers_final_drain(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    env = FakeEnv([b64(assistant_line("m", {"input_tokens": 5}) + b"\n")])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    tailer.request_stop()
    await asyncio.wait_for(tailer.run(), timeout=5)
    assert len(env.commands) == 1
    assert checkpoint_params(session)[-1]["input_tokens"] == 5


@pytest.mark.asyncio
async def test_cost_is_monotone_across_unpriceable_ticks(monkeypatch):
    session = patch_db(monkeypatch)
    prices = iter([0.5, None, 0.4, 0.9])
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: next(prices))
    chunks = [
        b64(assistant_line(f"m{i}", {"input_tokens": i + 1}) + b"\n") for i in range(4)
    ]
    env = FakeEnv(chunks)
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model="m")
    for _ in range(4):
        await tailer._tick()
    costs = [p["cost_usd"] for p in checkpoint_params(session)]
    assert costs == [0.5, 0.5, 0.5, 0.9]


@pytest.mark.asyncio
async def test_invalid_base64_raises_exec_error():
    env = FakeEnv([FakeResult(stdout="not-base64!!")])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    with pytest.raises(TailExecError):
        await tailer._tick()


@pytest.mark.asyncio
async def test_zero_rowcount_stops_tailer(monkeypatch):
    patch_db(monkeypatch, rowcount=0)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    env = FakeEnv([b64(assistant_line("m", {"input_tokens": 1}) + b"\n")])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    await tailer._tick()
    assert tailer._stop.is_set()
    assert tailer._last_written is None


@pytest.mark.asyncio
async def test_replaced_tailer_skips_checkpoint(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    env = FakeEnv([b64(assistant_line("m", {"input_tokens": 1}) + b"\n")])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    tailer.replaced = True
    await tailer._tick()
    assert session.stmts == []


@pytest.mark.asyncio
async def test_start_replaces_existing_tailer(monkeypatch):
    monkeypatch.setattr(live_tail.settings, "live_tail_enabled", True)
    monkeypatch.setattr(live_tail.settings, "live_tail_interval_sec", 60)
    env = FakeEnv([])
    kwargs = dict(trial_id="t1", environment=env, attempt=0, agent="claude-code", model=None)
    live_tail.start(**kwargs)
    old_tailer, old_task = live_tail._tailers["t1"]
    live_tail.start(**{**kwargs, "attempt": 1})
    new_tailer, new_task = live_tail._tailers["t1"]
    assert new_tailer is not old_tailer
    assert new_tailer.attempt == 1
    assert old_tailer.replaced and not new_tailer.replaced
    with contextlib.suppress(asyncio.CancelledError):
        await old_task
    assert live_tail._tailers.get("t1") == (new_tailer, new_task)
    await live_tail.shutdown("t1")
    assert "t1" not in live_tail._tailers
