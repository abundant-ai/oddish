"""Database tests for QA vote persistence and experiment membership."""

import os
import uuid

import pytest
from fastapi import HTTPException

from oddish.schemas import FeedbackCreate

URL = os.environ.get("ODDISH_DATABASE_URL")
pytestmark = pytest.mark.asyncio


async def _make_trial(session, *, run: str):
    from oddish.db.models import ExperimentModel, TaskModel, TrialModel

    experiment = ExperimentModel(name=f"exp-{run}")
    task = TaskModel(id=f"task-{run}", name=f"task-{run}", user="u", task_path="p")
    session.add_all([experiment, task])
    await session.flush()
    trial = TrialModel(
        id=f"trial-{run}",
        name=f"trial-{run}",
        task_id=task.id,
        experiment_id=experiment.id,
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5",
    )
    session.add(trial)
    await session.flush()
    return experiment, trial


async def test_qa_vote_persists_structured_subject():
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.core.feedback import create_feedback_core
    from oddish.db import get_session, init_db

    await init_db()
    async with get_session() as session:
        experiment, trial = await _make_trial(session, run=uuid.uuid4().hex[:8])
        row = await create_feedback_core(
            session,
            data=FeedbackCreate(
                target="qa_verdict",
                target_key="BAD_FAILURE",
                vote="disagree",
                trial_id=trial.id,
                body="The verifier evidence contradicts this.",
            ),
            experiment_id=experiment.id,
            org_id=None,
            user_id="user-1",
        )

        assert row.experiment_id == experiment.id
        assert row.trial_id == trial.id
        assert row.target == "qa_verdict"
        assert row.target_key == "BAD_FAILURE"
        assert row.vote == "disagree"
        assert row.created_by_user_id == "user-1"


async def test_foreign_experiment_cannot_accept_gathered_trial_feedback():
    if not URL:
        pytest.skip("ODDISH_DATABASE_URL not set")
    from oddish.core.feedback import create_feedback_core
    from oddish.db import experiment_trials, get_session, init_db
    from oddish.db.models import ExperimentModel

    await init_db()
    async with get_session() as session:
        _, trial = await _make_trial(session, run=uuid.uuid4().hex[:8])
        other = ExperimentModel(
            name=f"other-{uuid.uuid4().hex[:8]}", org_id="foreign-org"
        )
        session.add(other)
        await session.flush()
        await session.execute(
            experiment_trials.insert().values(
                experiment_id=other.id,
                trial_id=trial.id,
            )
        )

        with pytest.raises(HTTPException, match="trial not found") as excinfo:
            await create_feedback_core(
                session,
                data=FeedbackCreate(
                    target="qa_action_item",
                    target_key="item-1",
                    vote="agree",
                    trial_id=trial.id,
                ),
                experiment_id=other.id,
                org_id=None,
                user_id=None,
            )

        assert excinfo.value.status_code == 404
