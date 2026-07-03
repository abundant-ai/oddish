"""``last_runner_user_id`` on the dashboard experiments list.

The "Last run" column's task-based attribution (``tasks.user`` / the
``github_username`` tag) is stamped set-once at task creation, so appending
trials to a shared task keeps showing the task's original creator. The core
therefore also exposes the latest trial's ``billed_user_id`` (per-run
identity, stamped on every trial at submission) as ``last_runner_user_id``
so the hosted layer can resolve the actual latest runner.

Uses the rollback-per-test ``session`` fixture against the local Postgres,
with a run-scoped org id so the page query sees only this test's experiment.
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.dashboard import load_dashboard_experiments  # noqa: E402
from oddish.db.models import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    generate_id,
    utcnow,
)

_ORG = f"lastrunner-org-{uuid.uuid4().hex[:8]}"


def _trial(
    task: TaskModel,
    experiment: ExperimentModel,
    *,
    billed_user_id: str | None,
    created_at,
) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        experiment_id=experiment.id,
        org_id=_ORG,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
        status=TrialStatus.SUCCESS,
        billed_user_id=billed_user_id,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_last_runner_user_id_is_latest_trials_billed_user(session):
    """The newest trial's billed_user_id wins, not the task creator's strings."""
    now = utcnow()

    task = TaskModel(
        name=f"lastrunner-task-{_ORG}",
        org_id=_ORG,
        user="rishi@abundant.ai",  # set-once task creator; must NOT drive the id
        task_path="s3://tasks/lastrunner",
    )
    session.add(task)
    await session.flush()

    experiment = ExperimentModel(
        name=f"lastrunner-exp-{_ORG}", org_id=_ORG, last_activity_at=now
    )
    session.add(experiment)
    await session.flush()

    session.add_all(
        [
            # Older trial billed to the task creator...
            _trial(
                task,
                experiment,
                billed_user_id="user_rishi",
                created_at=now - timedelta(hours=2),
            ),
            # ...then an APPEND run billed to someone else: the latest trial.
            _trial(
                task,
                experiment,
                billed_user_id="user_charles",
                created_at=now - timedelta(hours=1),
            ),
        ]
    )
    await session.flush()

    rows, _has_more = await load_dashboard_experiments(
        session,
        org_id=_ORG,
        experiments_limit=10,
        experiments_offset=0,
        experiments_query=None,
        experiments_status="all",
    )

    assert len(rows) == 1
    assert rows[0]["id"] == experiment.id
    assert rows[0]["last_runner_user_id"] == "user_charles"


@pytest.mark.asyncio
async def test_last_runner_user_id_none_without_trials(session):
    now = utcnow()
    experiment = ExperimentModel(
        name=f"lastrunner-empty-{_ORG}", org_id=_ORG, last_activity_at=now
    )
    session.add(experiment)
    await session.flush()

    rows, _has_more = await load_dashboard_experiments(
        session,
        org_id=_ORG,
        experiments_limit=10,
        experiments_offset=0,
        experiments_query=None,
        experiments_status="all",
    )

    assert len(rows) == 1
    assert rows[0]["last_runner_user_id"] is None
