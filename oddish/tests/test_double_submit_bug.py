"""Reproduces the manifest re-trigger bug: re-submitting the SAME sweep config
for an unchanged task appends another batch of N trials instead of keeping the
task at N trials.

Expected (desired) behavior: submitting the same sweep twice leaves N trials.
Current (buggy) behavior: the second submission appends another N, giving 2N.

Run just this file:
    cd oddish
    set -a && source .env.local && set +a
    uv run pytest tests/test_double_submit_bug.py -v
"""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import TaskModel, TrialModel, get_session  # noqa: E402
from oddish.queue import create_task  # noqa: E402
from oddish.schemas import (  # noqa: E402
    AgentModelPair,
    TaskSubmission,
    TaskSweepSubmission,
    TrialSpec,
)

_RUN = uuid.uuid4().hex[:8]

# How many trials the manifest asks for per task.
N_TRIALS = 3
SWEEP_AGENT = "claude-code"
SWEEP_MODEL = "anthropic/claude-sonnet-4-6"


@pytest_asyncio.fixture
async def cleanup_task_ids():
    ids: list[str] = []
    yield ids
    async with get_session() as s:
        for tid in ids:
            # ON DELETE CASCADE removes trials + task_versions with the task.
            await s.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))


@pytest_asyncio.fixture
async def seeded_task_id(cleanup_task_ids):
    """Seed a task (via create_task so it gets current_version_id + a linked
    experiment, both required by the append path). Seeded with a single ``nop``
    trial that is NOT the sweep agent, so it won't be counted below."""
    task_id = f"double-submit-{_RUN}"
    async with get_session() as s:
        await create_task(
            s,
            TaskSubmission(
                name=f"double-submit-{_RUN}",
                task_path="s3://test-bucket/double-submit-fake-task",
                user="test",
                trials=[TrialSpec(agent="nop", model=None)],
            ),
            task_id=task_id,
        )
    cleanup_task_ids.append(task_id)
    yield task_id


async def _sweep_trials(session, task_id: str) -> list:
    """Count the non-probe trials this sweep is responsible for (its agent only),
    so the seed ``nop`` trial doesn't inflate the number."""
    rows = await session.execute(
        TrialModel.__table__.select().where(
            (TrialModel.task_id == task_id)
            & (TrialModel.agent == SWEEP_AGENT)
            & (TrialModel.is_probe.is_(False))
        )
    )
    return list(rows)


@pytest.mark.asyncio
async def test_resubmitting_identical_sweep_keeps_n_trials(seeded_task_id):
    """Submitting the same sweep twice should leave the task at N trials, not 2N."""
    from oddish.core.endpoints import create_task_sweep_core

    submission = TaskSweepSubmission(
        task_id=seeded_task_id,
        append_to_task=True,
        configs=[
            AgentModelPair(
                agent=SWEEP_AGENT,
                model=SWEEP_MODEL,
                n_trials=N_TRIALS,
            )
        ],
        user="test",
    )

    # First submission: creates N trials.
    async with get_session() as s:
        await create_task_sweep_core(s, submission=submission, org_id=None)
    async with get_session() as s:
        first = await _sweep_trials(s, seeded_task_id)
    assert (
        len(first) == N_TRIALS
    ), f"expected {N_TRIALS} trials after first submit, got {len(first)}"

    # Second submission: identical config, unchanged task. Should STILL be N.
    async with get_session() as s:
        await create_task_sweep_core(s, submission=submission, org_id=None)
    async with get_session() as s:
        second = await _sweep_trials(s, seeded_task_id)

    # This assert currently FAILS (got 2N) -> that failure is the bug reproduced.
    assert len(second) == N_TRIALS, (
        f"re-submitting the identical sweep should keep {N_TRIALS} trials, "
        f"but got {len(second)} (append stacked another batch)"
    )
