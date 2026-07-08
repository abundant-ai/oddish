import asyncio
import json

import pytest

from live_tail_fakes import FakeEnv, FakeResult, b64, patch_db, update_params
from oddish.workers.harbor import live_tail
from oddish.workers.harbor.live_tail import (
    ClaudeUsageFold,
    LiveTailer,
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
    params = update_params(session)
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
    params = update_params(session)
    assert params[-1]["cost_usd"] is None


@pytest.mark.asyncio
async def test_tick_rc_semantics():
    env = FakeEnv([FakeResult(return_code=1)])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    await tailer._tick()

    env = FakeEnv([FakeResult(return_code=127)])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    with pytest.raises(RuntimeError):
        await tailer._tick()

    env = FakeEnv([FakeResult(return_code=1)])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    tailer.offset = 10
    with pytest.raises(RuntimeError):
        await tailer._tick()


@pytest.mark.asyncio
async def test_run_disables_after_consecutive_failures(monkeypatch):
    monkeypatch.setattr(live_tail.settings, "live_tail_interval_sec", 0.001)
    env = FakeEnv([RuntimeError("boom")] * 10)
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    await asyncio.wait_for(tailer.run(), timeout=5)
    assert len(env.commands) == live_tail.MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_failure_cap_persists_pending_fold(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    monkeypatch.setattr(live_tail.settings, "live_tail_interval_sec", 0.001)
    env = FakeEnv([RuntimeError("boom")] * 10)
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    tailer.fold.feed_line(assistant_line("m", {"input_tokens": 2}))
    await asyncio.wait_for(tailer.run(), timeout=5)
    assert len(env.commands) == live_tail.MAX_CONSECUTIVE_FAILURES
    assert update_params(session)[-1]["input_tokens"] == 2


@pytest.mark.asyncio
async def test_stop_triggers_final_drain(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    env = FakeEnv([b64(assistant_line("m", {"input_tokens": 5}) + b"\n")])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
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
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model="m")
    for _ in range(4):
        await tailer._tick()
    costs = [p["cost_usd"] for p in update_params(session)]
    assert costs == [0.5, 0.5, 0.5, 0.9]


@pytest.mark.asyncio
async def test_stop_path_persists_fold_when_final_tick_fails(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    env = FakeEnv([RuntimeError("sandbox died")])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    tailer.fold.feed_line(assistant_line("m", {"input_tokens": 6}))
    tailer.request_stop()
    await asyncio.wait_for(tailer.run(), timeout=5)
    assert update_params(session)[-1]["input_tokens"] == 6


@pytest.mark.asyncio
async def test_cancelled_tailer_persists_fold_unless_replaced(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)

    class HangingEnv:
        async def exec(self, command, timeout_sec=None):
            await asyncio.Event().wait()

    for replaced, expected_writes in ((False, 1), (True, 0)):
        session.stmts.clear()
        tailer = LiveTailer(trial_id="t1", environment=HangingEnv(), attempt=0, model=None)
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
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    env = FakeEnv([FakeResult()])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    tailer.fold.feed_line(assistant_line("m", {"input_tokens": 3}))
    await tailer._tick()
    assert update_params(session)[-1]["input_tokens"] == 3


@pytest.mark.asyncio
async def test_invalid_base64_raises_exec_error():
    env = FakeEnv([FakeResult(stdout="not-base64!!")])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    with pytest.raises(RuntimeError):
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
