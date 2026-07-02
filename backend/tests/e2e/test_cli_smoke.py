from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import func, select, text

from .conftest import DB_URL, E2E_ENABLED, cli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (E2E_ENABLED and DB_URL),
        reason="e2e opt-in: set ODDISH_E2E=1 and ODDISH_DATABASE_URL",
    ),
]


async def test_p0_liveness(live_server):
    r = httpx.get(f"{live_server}/public/experiments", timeout=5.0)
    assert r.status_code == 200
    assert r.json() == []


async def test_p1_ls_shows_seeded_task(live_server, seeded):
    proc = cli(live_server, seeded["api_key"], "ls", "--json")
    assert proc.returncode == 0, proc.stderr
    items = json.loads(proc.stdout).get("items") or []
    assert any(t.get("id") == seeded["task_id"] for t in items)


async def test_p2_run_submits_queued_trial(live_server, seeded):
    from oddish.db import TrialModel, get_session

    proc = cli(
        live_server,
        seeded["api_key"],
        "run",
        "--task",
        seeded["task_id"],
        "--agent",
        "nop",
        "--n-trials",
        "1",
        "--background",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr

    async with get_session() as session:
        trial_count = await session.scalar(
            select(func.count())
            .select_from(TrialModel)
            .where(TrialModel.task_id == seeded["task_id"])
        )
        queued = await session.scalar(
            text(
                "select count(*) from worker_jobs w "
                "join trials t on t.id = w.subject_id "
                "where w.subject_table = 'trials' and w.status = 'QUEUED' "
                "and t.task_id = :task"
            ),
            {"task": seeded["task_id"]},
        )
    assert trial_count == 1
    assert queued == 1


async def test_p3_status_shows_queued_trial(live_server, seeded):
    submit = cli(
        live_server,
        seeded["api_key"],
        "run",
        "--task",
        seeded["task_id"],
        "--agent",
        "nop",
        "--n-trials",
        "1",
        "--background",
        "--json",
    )
    assert submit.returncode == 0, submit.stderr

    proc = cli(live_server, seeded["api_key"], "status", seeded["task_id"], "--json")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    trials = payload.get("trials") or []
    assert len(trials) == 1
