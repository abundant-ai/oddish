"""Tests for the org-wide probe-runs aggregation helper.

Exercises ``oddish.core.experiments.list_org_probes_core`` against the real
local Postgres (same pattern as ``test_experiment_probes_api.py``: seed rows,
yield, teardown via id-scoped deletes).

Run with your backend env sourced:

    set -a && source .env && set +a && uv run pytest tests/test_org_probes_api.py -v
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from oddish.core.experiments import list_org_probes_core
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    get_session,
)
from models import OrganizationModel


async def _cleanup(*, trial_ids, task_ids, experiment_ids, org_ids) -> None:
    """Hard-delete seed rows in FK-safe order (trials → tasks → experiments → orgs)."""
    async with get_session() as session:
        if trial_ids:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.id.in_(trial_ids))
            )
        if task_ids:
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id.in_(task_ids))
            )
        if experiment_ids:
            await session.execute(
                ExperimentModel.__table__.delete().where(
                    ExperimentModel.id.in_(experiment_ids)
                )
            )
        if org_ids:
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id.in_(org_ids)
                )
            )


@pytest_asyncio.fixture
async def probed_org():
    """An org with:
      - task A: two probe trials (one newer) + one non-probe trial
      - task B: one probe trial (oldest of all probes)
      - task C: only a non-probe trial (must be omitted)
    Plus a second org with a probe trial (must be isolated out).
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_op_{suffix}"
    other_org_id = f"org_op_other_{suffix}"
    exp_id = f"exp_op_{suffix}"
    other_exp_id = f"exp_op_other_{suffix}"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    task_a = f"task_op_a_{suffix}"
    task_b = f"task_op_b_{suffix}"
    task_c = f"task_op_c_{suffix}"
    other_task = f"task_op_other_{suffix}"

    a_old = f"trial_a_old_{suffix}"
    a_new = f"trial_a_new_{suffix}"
    a_real = f"trial_a_real_{suffix}"
    b_probe = f"trial_b_{suffix}"
    c_real = f"trial_c_{suffix}"
    other_probe = f"trial_other_{suffix}"

    # experiment_id is NOT NULL on TrialModel — every trial needs a valid one.
    def trial(tid, task_id, oid, eid, created, *, is_probe, status=TrialStatus.SUCCESS):
        return TrialModel(
            id=tid,
            name=f"{task_id}-{tid}",
            task_id=task_id,
            experiment_id=eid,
            org_id=oid,
            agent="claude-code",
            provider="anthropic",
            model="anthropic/claude-sonnet-4-6",
            queue_key="test-op",
            status=status,
            origin=TrialOrigin.ODDISH,
            is_probe=is_probe,
            created_at=created,
        )

    async with get_session() as session:
        for oid in (org_id, other_org_id):
            session.add(
                OrganizationModel(id=oid, name=f"Org {oid}", slug=oid.replace("_", "-"))
            )
        session.add(ExperimentModel(id=exp_id, name=f"op-{suffix}", org_id=org_id))
        session.add(
            ExperimentModel(
                id=other_exp_id, name=f"op-other-{suffix}", org_id=other_org_id
            )
        )
        for tid, oid in (
            (task_a, org_id),
            (task_b, org_id),
            (task_c, org_id),
            (other_task, other_org_id),
        ):
            session.add(
                TaskModel(
                    id=tid,
                    name=f"name-{tid}",
                    user="test",
                    task_path="/tmp/fake",
                    org_id=oid,
                )
            )
        await session.flush()
        session.add(trial(a_old, task_a, org_id, exp_id, base, is_probe=True))
        session.add(
            trial(
                a_new,
                task_a,
                org_id,
                exp_id,
                base + timedelta(hours=2),
                is_probe=True,
                status=TrialStatus.RUNNING,
            )
        )
        session.add(
            trial(
                a_real,
                task_a,
                org_id,
                exp_id,
                base + timedelta(hours=3),
                is_probe=False,
            )
        )
        session.add(
            trial(
                b_probe,
                task_b,
                org_id,
                exp_id,
                base - timedelta(hours=5),
                is_probe=True,
            )
        )
        session.add(trial(c_real, task_c, org_id, exp_id, base, is_probe=False))
        session.add(
            trial(
                other_probe, other_task, other_org_id, other_exp_id, base, is_probe=True
            )
        )

    yield {"org_id": org_id, "task_a": task_a, "task_b": task_b, "task_c": task_c}

    await _cleanup(
        trial_ids=[a_old, a_new, a_real, b_probe, c_real, other_probe],
        task_ids=[task_a, task_b, task_c, other_task],
        experiment_ids=[exp_id, other_exp_id],
        org_ids=[org_id, other_org_id],
    )


@pytest.mark.asyncio
async def test_list_org_probes_groups_counts_and_orders(probed_org):
    async with get_session() as session:
        rows = await list_org_probes_core(session, org_id=probed_org["org_id"])

    # Only tasks A and B have probe trials; C is omitted, other org isolated out.
    assert [r.task_id for r in rows] == [probed_org["task_a"], probed_org["task_b"]]

    a = rows[0]
    assert a.run_count == 2  # two probe trials, non-probe excluded
    assert a.last_status == "running"  # status of the most recent probe trial
    b = rows[1]
    assert b.run_count == 1
    # A's most recent probe is newer than B's only probe → A first.
    assert a.last_run_at > b.last_run_at
