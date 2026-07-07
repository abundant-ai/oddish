import asyncio
import base64
import contextlib
import json

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import Insert as PGInsert
from sqlalchemy.exc import SQLAlchemyError

from oddish.workers.harbor import live_tail
from oddish.workers.harbor.live_tail import ClaudeUsageFold, LiveTailer
from oddish.workers.queue import cleanup


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
    def __init__(self, rowcount=1, fail_inserts=False):
        self.stmts = []
        self.params = []
        self.rowcount = rowcount
        self.fail_inserts = fail_inserts

    async def execute(self, stmt, params=None):
        if self.fail_inserts and isinstance(stmt, PGInsert):
            raise RuntimeError("insert failed")
        self.stmts.append(stmt)
        self.params.append(params)
        return FakeExecuteResult(self.rowcount)


def patch_db(monkeypatch, module=live_tail, **kwargs):
    session = FakeSession(**kwargs)

    @contextlib.asynccontextmanager
    async def fake_get_session():
        yield session

    monkeypatch.setattr(module, "get_session", fake_get_session)
    return session


def insert_stmts(session):
    return [s for s in session.stmts if isinstance(s, PGInsert)]


def update_params(session):
    return [
        dict(s.compile().params)
        for s in session.stmts
        if not isinstance(s, PGInsert)
    ]


def insert_rows(stmt):
    params = stmt.compile(dialect=postgresql.dialect()).params
    rows: dict[int, dict] = {}
    for key, value in params.items():
        name, _, idx = key.rpartition("_m")
        rows.setdefault(int(idx), {})[name] = value
    return [rows[i] for i in sorted(rows)]


def line(obj) -> bytes:
    return json.dumps(obj).encode()


def assistant_content(blocks, msg_id="msg_1", usage=None, model="claude-opus-4-8"):
    message = {"id": msg_id, "model": model, "content": blocks}
    if usage is not None:
        message["usage"] = usage
    return line({"type": "assistant", "message": message})


def text_line(text, msg_id="msg_1", usage=None):
    return assistant_content([{"type": "text", "text": text}], msg_id, usage)


def tool_result_line(content, is_error=False):
    block = {"type": "tool_result", "tool_use_id": "tu_1", "content": content}
    if is_error:
        block["is_error"] = True
    return line({"type": "user", "message": {"content": [block]}})


def test_feed_line_renders_display_kinds():
    fold = ClaudeUsageFold()
    rendered = fold.feed_line(
        assistant_content(
            [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
            ],
            usage={"input_tokens": 1},
        )
    )
    assert [e["kind"] for e in rendered] == ["message", "tool_use"]
    assert rendered[0]["payload"] == {"text": "hello"}
    assert rendered[1]["payload"] == {
        "input": json.dumps({"command": "ls"}),
        "name": "Bash",
    }
    assert fold.usage_by_id == {"msg_1": {"input_tokens": 1}}

    assert fold.feed_line(tool_result_line("output text")) == [
        {"kind": "tool_result", "payload": {"content": "output text"}}
    ]
    assert fold.feed_line(
        line({"type": "result", "result": "all done", "subtype": "success"})
    ) == [{"kind": "summary", "payload": {"text": "all done"}}]


def test_feed_line_renders_nothing_for_non_transcript_lines():
    fold = ClaudeUsageFold()
    assert fold.feed_line(b"") == []
    assert fold.feed_line(b"not json {") == []
    assert fold.feed_line(line({"type": "system"})) == []
    assert fold.feed_line(line({"type": "user", "message": {"content": "raw"}})) == []
    assert fold.feed_line(line({"type": "assistant", "message": {"id": "m"}})) == []


def test_tool_result_payload_truncated():
    fold = ClaudeUsageFold()
    [event] = fold.feed_line(tool_result_line("x" * 5000))
    assert event["payload"]["content"] == "x" * live_tail.PAYLOAD_CLIP_CHARS
    assert event["payload"]["truncated"] is True

    [event] = fold.feed_line(
        tool_result_line([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    )
    assert event["payload"] == {"content": "a\nb"}

    [event] = fold.feed_line(tool_result_line("boom", is_error=True))
    assert event["payload"] == {"content": "boom", "is_error": True}


def test_message_text_truncated():
    fold = ClaudeUsageFold()
    [event] = fold.feed_line(text_line("y" * 3000))
    assert len(event["payload"]["text"]) == live_tail.PAYLOAD_CLIP_CHARS
    assert event["payload"]["truncated"] is True


@pytest.mark.asyncio
async def test_seq_dense_per_attempt_across_ticks(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    tick1 = (
        text_line("a", usage={"input_tokens": 1})
        + b"\n"
        + tool_result_line("out")
        + b"\n"
    )
    tick2 = text_line("b", msg_id="msg_2", usage={"input_tokens": 2}) + b"\n"
    env = FakeEnv([b64(tick1), b64(tick2)])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=3, model=None)
    await tailer._tick()
    await tailer._tick()
    inserts = insert_stmts(session)
    assert len(inserts) == 2
    rows = insert_rows(inserts[0]) + insert_rows(inserts[1])
    assert [(r["trial_id"], r["attempt"], r["seq"], r["kind"]) for r in rows] == [
        ("t1", 3, 1, "message"),
        ("t1", 3, 2, "tool_result"),
        ("t1", 3, 3, "message"),
    ]
    sql = str(inserts[0].compile(dialect=postgresql.dialect()))
    assert "INSERT INTO trial_events" in sql
    assert "ON CONFLICT DO NOTHING" in sql


@pytest.mark.asyncio
async def test_cap_writes_one_summary_row_and_keeps_checkpoints(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    monkeypatch.setattr(live_tail, "MAX_TRIAL_EVENTS", 2)
    tick1 = b"".join(
        text_line(f"m{i}", msg_id=f"msg_{i}", usage={"input_tokens": i + 1}) + b"\n"
        for i in range(4)
    )
    tick2 = text_line("late", msg_id="msg_9", usage={"input_tokens": 50}) + b"\n"
    env = FakeEnv([b64(tick1), b64(tick2)])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)

    await tailer._tick()
    [insert] = insert_stmts(session)
    rows = insert_rows(insert)
    assert [(r["seq"], r["kind"]) for r in rows] == [
        (1, "message"),
        (2, "message"),
        (3, "summary"),
    ]
    assert rows[-1]["payload"]["capped"] is True
    assert tailer.capped

    await tailer._tick()
    assert len(insert_stmts(session)) == 1
    assert update_params(session)[-1]["input_tokens"] == 60


@pytest.mark.asyncio
async def test_replaced_guard_suppresses_event_inserts(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    env = FakeEnv([b64(text_line("hi", usage={"input_tokens": 1}) + b"\n")])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    tailer.replaced = True
    await tailer._tick()
    assert session.stmts == []
    assert tailer.pending_events == []
    assert tailer.seq == 0


@pytest.mark.asyncio
async def test_flush_failure_retains_pending_for_retry(monkeypatch):
    session = patch_db(monkeypatch, fail_inserts=True)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    env = FakeEnv([b64(text_line("hi", usage={"input_tokens": 1}) + b"\n")])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    with pytest.raises(RuntimeError):
        await tailer._tick()
    assert len(tailer.pending_events) == 1

    session.fail_inserts = False
    await tailer._tick()
    [insert] = insert_stmts(session)
    assert insert_rows(insert)[0]["seq"] == 1
    assert tailer.pending_events == []


@pytest.mark.asyncio
async def test_final_drain_flushes_carry_events(monkeypatch):
    session = patch_db(monkeypatch)
    monkeypatch.setattr(live_tail, "estimate_cost_usd", lambda *_a, **_k: None)
    env = FakeEnv([b64(text_line("tail", usage={"input_tokens": 1}))])
    tailer = LiveTailer(trial_id="t1", environment=env, attempt=0, model=None)
    tailer.request_stop()
    await asyncio.wait_for(tailer.run(), timeout=5)
    [insert] = insert_stmts(session)
    assert insert_rows(insert)[0]["kind"] == "message"


def test_feed_line_never_raises_on_typed_garbage():
    fold = ClaudeUsageFold()
    garbage = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": 5}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": {"a": 1}}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": True}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": 7, "input": 3}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": {"x": 1}}]}},
        {"type": "result", "result": ["a", "b"]},
    ]
    rendered = [
        event
        for line in garbage
        for event in fold.feed_line(json.dumps(line).encode())
    ]
    assert all(isinstance(e["payload"], dict) for e in rendered)
    assert len(rendered) == len(garbage)


@pytest.mark.asyncio
async def test_start_replacement_inherits_seq_and_cap(monkeypatch):
    patch_db(monkeypatch)
    monkeypatch.setattr(live_tail.settings, "live_tail_enabled", True)
    monkeypatch.setattr(live_tail.settings, "live_tail_interval_sec", 60)
    kwargs = dict(
        trial_id="t1",
        environment=FakeEnv([]),
        attempt=0,
        agent="claude-code",
        model=None,
    )
    live_tail.start(**kwargs)
    old_tailer, old_task = live_tail._tailers["t1"]
    old_tailer.seq = 7
    old_tailer.capped = True
    old_tailer.pending_events.append({"seq": 7, "kind": "message", "payload": {}})
    live_tail.start(**kwargs)
    new_tailer, _ = live_tail._tailers["t1"]
    assert new_tailer.seq == 7 and new_tailer.capped
    assert new_tailer.pending_events == [{"seq": 7, "kind": "message", "payload": {}}]
    assert new_tailer.pending_events is not old_tailer.pending_events
    old_tailer._buffer_events([{"kind": "message", "payload": {}}])
    assert len(old_tailer.pending_events) == 1
    with contextlib.suppress(asyncio.CancelledError):
        await old_task
    await live_tail.shutdown("t1")
    assert "t1" not in live_tail._tailers


@pytest.mark.asyncio
async def test_shutdown_after_task_completion_returns_attempt(monkeypatch):
    patch_db(monkeypatch)
    monkeypatch.setattr(live_tail.settings, "live_tail_enabled", True)
    monkeypatch.setattr(live_tail.settings, "live_tail_interval_sec", 60)
    live_tail.start(
        trial_id="t1",
        environment=FakeEnv([]),
        attempt=3,
        agent="claude-code",
        model=None,
    )
    tailer, task = live_tail._tailers["t1"]
    tailer.request_stop()
    await asyncio.wait_for(task, timeout=5)
    assert "t1" in live_tail._tailers
    assert await live_tail.shutdown("t1") == 3
    assert "t1" not in live_tail._tailers


@pytest.mark.asyncio
async def test_purge_events_deletes_rows_bounded_by_attempt(monkeypatch):
    session = patch_db(monkeypatch)
    await live_tail.purge_events("t1", 2)
    [stmt] = session.stmts
    compiled = stmt.compile(dialect=postgresql.dialect())
    assert "DELETE FROM trial_events" in str(compiled)
    assert "attempt <=" in str(compiled)
    assert compiled.params == {"trial_id_1": "t1", "attempt_1": 2}


@pytest.mark.asyncio
async def test_purge_events_skips_without_attempt(monkeypatch):
    session = patch_db(monkeypatch)
    await live_tail.purge_events("t1", None)
    assert session.stmts == []


@pytest.mark.asyncio
async def test_purge_events_swallows_errors(monkeypatch):
    @contextlib.asynccontextmanager
    async def broken():
        raise RuntimeError("db down")
        yield

    monkeypatch.setattr(live_tail, "get_session", broken)
    await live_tail.purge_events("t1", 0)


@pytest.mark.asyncio
async def test_ttl_sweep_deletes_expired_events(monkeypatch):
    session = patch_db(monkeypatch, module=cleanup, rowcount=4)
    assert await cleanup.purge_stale_trial_events() == 4
    [stmt] = session.stmts
    sql = str(stmt)
    assert "DELETE FROM trial_events" in sql
    assert "finished_at IS NOT NULL" in sql
    assert "finished_at < NOW() - make_interval" in sql
    assert session.params == [{"ttl_hours": cleanup.TRIAL_EVENTS_TTL_HOURS}]


@pytest.mark.asyncio
async def test_ttl_sweep_is_best_effort(monkeypatch):
    @contextlib.asynccontextmanager
    async def broken():
        raise SQLAlchemyError("db down")
        yield

    monkeypatch.setattr(cleanup, "get_session", broken)
    assert await cleanup.purge_stale_trial_events() == 0
