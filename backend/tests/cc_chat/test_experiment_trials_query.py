import pytest
from sqlalchemy import text
from tests.cc_chat.conftest import seed_task_with_trials, ORG
from oddish.core.public_helpers import list_experiment_trials

pytestmark = pytest.mark.asyncio


async def test_lists_all_experiment_trials_with_summary_fields(db):
    # seed_task_with_trials creates experiment "exp_task_1" with task "demo-task".
    await seed_task_with_trials(db, versions=(1, 2), trials_per_version=2)
    async with db() as s:
        rows = await list_experiment_trials(s, "exp_task_1", org_id=ORG)
    assert {r.trial_id for r in rows} == {
        "task_1-10", "task_1-11", "task_1-20", "task_1-21",
    }
    r = next(r for r in rows if r.trial_id == "task_1-10")
    assert r.task_name == "demo-task"
    assert r.is_probe is False
    assert r.has_trajectory is False


async def test_excludes_other_orgs(db):
    await seed_task_with_trials(db, versions=(1,), trials_per_version=1)
    async with db() as s:
        rows = await list_experiment_trials(s, "exp_task_1", org_id="org_other")
    assert rows == []


async def test_flags_probe_trials(db):
    seeded = await seed_task_with_trials(db, versions=(1,), trials_per_version=2)
    probe_id = seeded[1][0]  # first trial of version 1 == "task_1-10"
    async with db() as s:
        await s.execute(
            text("update trials set is_probe = true where id = :id"), {"id": probe_id}
        )
        await s.commit()
    async with db() as s:
        rows = await list_experiment_trials(s, "exp_task_1", org_id=ORG)
    assert {r.trial_id for r in rows if r.is_probe} == {probe_id}
