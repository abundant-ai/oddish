from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from oddish.core.sharing import public as public_sharing
from oddish.core.sharing.public import get_public_trial_live
from oddish.core.trial_live import LIVE_EVENTS_PAGE_LIMIT, read_trial_live


def make_trial(**overrides):
    base = dict(
        id="task-1-0",
        attempts=2,
        input_tokens=100,
        cache_tokens=40,
        cache_write_tokens=10,
        output_tokens=50,
        cost_usd=1.25,
        harbor_stage="agent_running",
        finished_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_event(seq, kind="message"):
    return SimpleNamespace(
        seq=seq,
        kind=kind,
        payload={"text": f"e{seq}"},
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
    )


class FakeSession:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.stmts = []

    async def scalars(self, stmt):
        self.stmts.append(stmt)
        rows = self.rows
        return SimpleNamespace(all=lambda: rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass


@pytest.mark.asyncio
async def test_read_trial_live_response_shape():
    trial = make_trial()
    session = FakeSession([make_event(1), make_event(2, kind="tool_use")])
    resp = await read_trial_live(session, trial, attempt=2, after_seq=0)
    assert resp["attempt"] == 2
    assert [e["seq"] for e in resp["events"]] == [1, 2]
    assert resp["events"][0]["payload"] == {"text": "e1"}
    assert resp["events"][1]["kind"] == "tool_use"
    assert resp["events"][0]["created_at"] == datetime(2026, 7, 7, tzinfo=timezone.utc)
    assert resp["next_seq"] == 2
    assert resp["usage"] == {
        "input_tokens": 100,
        "cache_tokens": 40,
        "cache_write_tokens": 10,
        "output_tokens": 50,
        "cost_usd": 1.25,
    }
    assert resp["harbor_stage"] == "agent_running"
    assert resp["done"] is False


@pytest.mark.asyncio
async def test_read_trial_live_empty_and_done():
    trial = make_trial(finished_at=datetime.now(timezone.utc))
    session = FakeSession([])
    resp = await read_trial_live(session, trial, after_seq=17)
    assert resp["events"] == []
    assert resp["next_seq"] == 17
    assert resp["done"] is True


@pytest.mark.asyncio
async def test_read_trial_live_queries_current_attempt():
    trial = make_trial()
    session = FakeSession([make_event(5), make_event(6)])
    resp = await read_trial_live(session, trial, attempt=2, after_seq=4)
    [stmt] = session.stmts
    compiled = stmt.compile()
    assert compiled.params["trial_id_1"] == "task-1-0"
    assert compiled.params["attempt_1"] == 2
    assert compiled.params["seq_1"] == 4
    assert LIVE_EVENTS_PAGE_LIMIT in compiled.params.values()
    sql = str(compiled)
    assert "ORDER BY trial_events.seq" in sql
    assert resp["next_seq"] == 6
    assert [e["seq"] for e in resp["events"]] == [5, 6]


@pytest.mark.asyncio
async def test_read_trial_live_stale_attempt_resets_cursor():
    trial = make_trial(attempts=3)
    session = FakeSession([])
    resp = await read_trial_live(session, trial, attempt=1, after_seq=250)
    [stmt] = session.stmts
    params = stmt.compile().params
    assert params["attempt_1"] == 3
    assert params["seq_1"] == 0
    assert resp["attempt"] == 3
    assert resp["next_seq"] == 0


@pytest.mark.asyncio
async def test_read_trial_live_no_attempt_param_keeps_cursor():
    trial = make_trial()
    session = FakeSession([])
    resp = await read_trial_live(session, trial, after_seq=7)
    [stmt] = session.stmts
    assert stmt.compile().params["seq_1"] == 7
    assert resp["next_seq"] == 7


@pytest.mark.asyncio
async def test_public_live_reads_only_token_scoped_trial(monkeypatch):
    trial = make_trial()
    session = FakeSession([make_event(5)])

    async def get_public_trial(resolved_session, public_token, trial_id):
        assert resolved_session is session
        return trial if (public_token, trial_id) == ("share-token", trial.id) else None

    monkeypatch.setattr(public_sharing, "get_session", lambda: session)
    monkeypatch.setattr(
        public_sharing, "get_public_trial_for_experiment", get_public_trial
    )

    resp = await get_public_trial_live(
        "share-token", trial.id, attempt=trial.attempts, after_seq=4
    )

    assert [event["seq"] for event in resp["events"]] == [5]
    assert resp["next_seq"] == 5

    with pytest.raises(HTTPException) as exc:
        await get_public_trial_live("share-token", "private-trial", after_seq=0)

    assert exc.value.status_code == 404
