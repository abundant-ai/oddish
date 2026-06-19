"""Adding a new model to an existing sweep should trigger a full N trials for
that model while leaving the existing model's trials untouched.

Scenario (inspired by abundant-ai/experiments#522): a sweep runs task T with
model A.  The manifest is later updated to also include model B.  Re-triggering
should produce exactly N trials for model B (a new 'column' in the experiment)
and leave model A's trial count at N.

Run just this file:
    cd oddish
    set -a && source .env.local && set +a
    uv run pytest tests/test_new_model_reruns.py -v
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

N_TRIALS = 2
AGENT = "claude-code"
MODEL_A = "anthropic/claude-sonnet-4-5"
MODEL_B = "anthropic/claude-sonnet-4-6"


@pytest_asyncio.fixture
async def cleanup_task_ids():
    ids: list[str] = []
    yield ids
    async with get_session() as s:
        for tid in ids:
            await s.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))


@pytest_asyncio.fixture
async def seeded_task_id(cleanup_task_ids):
    """Seed a bare task so it has current_version_id + a linked experiment
    (both required by the append path). No sweep trials yet."""
    task_id = f"new-model-rerun-{_RUN}"
    async with get_session() as s:
        await create_task(
            s,
            TaskSubmission(
                name=f"new-model-rerun-{_RUN}",
                task_path="s3://test-bucket/new-model-rerun-fake-task",
                user="test",
                trials=[TrialSpec(agent="nop", model=None)],
            ),
            task_id=task_id,
        )
    cleanup_task_ids.append(task_id)
    yield task_id


async def _trials_for_model(session, task_id: str, model_raw: str) -> list:
    """Return non-probe trials for a specific (agent, raw-model) pair.

    Normalizes ``model_raw`` the same way the write path does so the comparison
    matches the stored column value.
    """
    from oddish.config import settings

    norm = settings.normalize_trial_model(AGENT, model_raw)
    rows = await session.execute(
        TrialModel.__table__.select().where(
            (TrialModel.task_id == task_id)
            & (TrialModel.agent == AGENT)
            & (TrialModel.model == norm)
            & (TrialModel.is_probe.is_(False))
        )
    )
    return list(rows)


@pytest.mark.asyncio
async def test_new_model_gets_full_n_trials(seeded_task_id):
    """Adding model B to a manifest that already ran model A should create N
    trials for model B without touching model A's count."""
    from oddish.core.endpoints import create_task_sweep_core

    submission_a = TaskSweepSubmission(
        task_id=seeded_task_id,
        append_to_task=True,
        configs=[AgentModelPair(agent=AGENT, model=MODEL_A, n_trials=N_TRIALS)],
        user="test",
    )

    async with get_session() as s:
        await create_task_sweep_core(s, submission=submission_a, org_id=None)
    async with get_session() as s:
        after_first_a = await _trials_for_model(s, seeded_task_id, MODEL_A)
        after_first_b = await _trials_for_model(s, seeded_task_id, MODEL_B)

    assert len(after_first_a) == N_TRIALS, (
        f"expected {N_TRIALS} trials for model A after first submit, got {len(after_first_a)}"
    )
    assert len(after_first_b) == 0, (
        f"expected 0 trials for model B before it was added, got {len(after_first_b)}"
    )

    submission_ab = TaskSweepSubmission(
        task_id=seeded_task_id,
        append_to_task=True,
        configs=[
            AgentModelPair(agent=AGENT, model=MODEL_A, n_trials=N_TRIALS),
            AgentModelPair(agent=AGENT, model=MODEL_B, n_trials=N_TRIALS),
        ],
        user="test",
    )
    async with get_session() as s:
        await create_task_sweep_core(s, submission=submission_ab, org_id=None)
    async with get_session() as s:
        after_second_a = await _trials_for_model(s, seeded_task_id, MODEL_A)
        after_second_b = await _trials_for_model(s, seeded_task_id, MODEL_B)

    assert len(after_second_a) == N_TRIALS, (
        f"model A should still have {N_TRIALS} trials (reconcile is idempotent), "
        f"got {len(after_second_a)}"
    )
    assert len(after_second_b) == N_TRIALS, (
        f"model B should have {N_TRIALS} trials after being added to the manifest, "
        f"got {len(after_second_b)} — reconcile failed to treat a new model as 0 existing"
    )


@pytest.mark.asyncio
async def test_new_experiment_gets_full_n_trials(seeded_task_id):
    """Re-submitting the same task+model to a different experiment should create
    a full N trials in the new experiment, not 0 (shortfall against the old
    experiment's count)."""
    from oddish.core.endpoints import create_task_sweep_core

    exp_a = f"exp-a-{_RUN}"
    exp_b = f"exp-b-{_RUN}"

    submission_exp_a = TaskSweepSubmission(
        task_id=seeded_task_id,
        append_to_task=True,
        experiment_id=exp_a,
        configs=[AgentModelPair(agent=AGENT, model=MODEL_A, n_trials=N_TRIALS)],
        user="test",
    )

    async with get_session() as s:
        await create_task_sweep_core(s, submission=submission_exp_a, org_id=None)
    async with get_session() as s:
        after_exp_a = await _trials_for_model(s, seeded_task_id, MODEL_A)
    assert len(after_exp_a) == N_TRIALS, (
        f"expected {N_TRIALS} trials in exp-a, got {len(after_exp_a)}"
    )

    submission_exp_b = TaskSweepSubmission(
        task_id=seeded_task_id,
        append_to_task=True,
        experiment_id=exp_b,
        configs=[AgentModelPair(agent=AGENT, model=MODEL_A, n_trials=N_TRIALS)],
        user="test",
    )
    async with get_session() as s:
        await create_task_sweep_core(s, submission=submission_exp_b, org_id=None)

    from oddish.db import TrialModel
    from sqlalchemy import select
    async with get_session() as s:
        from oddish.config import settings
        norm = settings.normalize_trial_model(AGENT, MODEL_A)
        result = await s.execute(
            select(TrialModel.experiment_id, TrialModel.id)
            .where(
                TrialModel.task_id == seeded_task_id,
                TrialModel.agent == AGENT,
                TrialModel.model == norm,
                TrialModel.is_probe.is_(False),
            )
        )
        rows = list(result)

    from collections import Counter
    counts_by_exp = Counter(r[0] for r in rows)

    from oddish.queue import get_or_create_experiment
    async with get_session() as s:
        exp_a_obj = await get_or_create_experiment(s, exp_a, org_id=None)
        exp_b_obj = await get_or_create_experiment(s, exp_b, org_id=None)

    assert counts_by_exp[exp_a_obj.id] == N_TRIALS, (
        f"exp-a should still have {N_TRIALS} trials, got {counts_by_exp[exp_a_obj.id]}"
    )
    assert counts_by_exp[exp_b_obj.id] == N_TRIALS, (
        f"exp-b should have {N_TRIALS} trials (new experiment = 0 existing), "
        f"got {counts_by_exp[exp_b_obj.id]}"
    )
