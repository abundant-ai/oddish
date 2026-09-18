from __future__ import annotations

import pytest
from sqlalchemy import insert, select

from oddish.core.dashboard import (
    _build_aggregates_for_experiment_ids,
    _experiment_row_passes_status_filter,
)
from oddish.core.verdict_state import INSUFFICIENT_EVIDENCE_ERROR
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    generate_id,
    task_experiments,
)


def _task(name: str, *, verdict_error: str) -> TaskModel:
    return TaskModel(
        name=name,
        org_id="org1",
        user="tester",
        task_path=f"s3://tasks/{name}",
        run_analysis=True,
        verdict_status=VerdictStatus.FAILED,
        verdict_error=verdict_error,
    )


def _trial(task: TaskModel, experiment: ExperimentModel) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        experiment_id=experiment.id,
        org_id="org1",
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
        status=TrialStatus.SUCCESS,
    )


@pytest.mark.asyncio
async def test_missing_evidence_counts_as_pending_verdict_not_failed(session):
    no_evidence = _task(
        "verdict-bucket-no-evidence", verdict_error=INSUFFICIENT_EVIDENCE_ERROR
    )
    crashed = _task("verdict-bucket-crashed", verdict_error="worker crashed")
    experiment = ExperimentModel(name="verdict-buckets", org_id="org1")
    session.add_all([no_evidence, crashed, experiment])
    await session.flush()
    session.add_all([_trial(no_evidence, experiment), _trial(crashed, experiment)])
    await session.execute(
        insert(task_experiments).values(
            [
                {"task_id": task.id, "experiment_id": experiment.id}
                for task in (no_evidence, crashed)
            ]
        )
    )
    await session.flush()

    task_agg, _, _ = _build_aggregates_for_experiment_ids(
        [experiment.id], org_id="org1"
    )
    row = (
        (
            await session.execute(
                select(task_agg).where(task_agg.c.experiment_id == experiment.id)
            )
        )
        .mappings()
        .one()
    )
    assert row["task_count"] == 2
    assert row["verdict_failed"] == 1
    assert row["verdict_pending"] == 1

    only_missing_evidence = {**row, "verdict_failed": 0, "failed_trials": 0}
    assert _experiment_row_passes_status_filter(
        only_missing_evidence, status_filter="pending-verdict"
    )
    assert not _experiment_row_passes_status_filter(
        only_missing_evidence, status_filter="failed"
    )
