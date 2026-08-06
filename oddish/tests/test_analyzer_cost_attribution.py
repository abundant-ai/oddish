"""``AnalyzerBlock._cost_attribution`` — the subject columns on a cost row.

Custom QA runs through ``analyzer_block_handler`` used to record a cost row
with every scope column NULL: the handler passes a non-NULL
``attribution_org_id`` (short-circuiting subject resolution) and an
``analyzer_id`` that is an AnalyzerRun id, matching no other table. The
run's own ``scope_type``/``scope_id`` knew the subject and were discarded.

Cohort blocks legitimately span many trials and still resolve to NULL.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.blocks.analyzer.analyzer_block import (  # noqa: E402
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType  # noqa: E402
from oddish.db.models import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    generate_id,
    utcnow,
)

_ORG = f"qaattr-org-{uuid.uuid4().hex[:8]}"


async def _trial_fixture(session) -> tuple[TaskModel, ExperimentModel, TrialModel]:
    task = TaskModel(
        name=f"qaattr-task-{_ORG}",
        org_id=_ORG,
        user="tester",
        task_path=f"s3://tasks/qaattr-{_ORG}",
    )
    session.add(task)
    await session.flush()

    experiment = ExperimentModel(
        name=f"qaattr-exp-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add(experiment)
    await session.flush()

    trial_id = generate_id()
    trial = TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        experiment_id=experiment.id,
        org_id=_ORG,
        agent="claude-code",
        provider="anthropic",
        queue_key="anthropic/claude-opus-4-8",
        model="claude-opus-4-8",
        status=TrialStatus.SUCCESS,
        billed_user_id="user-abc",
        created_at=utcnow(),
    )
    session.add(trial)
    await session.flush()
    return task, experiment, trial


def _block(**kwargs) -> AnalyzerBlock:
    return AnalyzerBlock(
        analyzer_type=AnalyzerType.CUSTOM_QA,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(input={}),
        prompt="grade this",
        **kwargs,
    )


@pytest.mark.asyncio
async def test_trial_scope_resolves_trial_experiment_and_billed_user(session):
    """The fix: a trial-scoped custom QA run charges the trial it graded."""
    _task, experiment, trial = await _trial_fixture(session)

    block = _block(
        subject_type="trial",
        subject_id=trial.id,
        attribution_org_id=_ORG,
    )
    attribution = await block._cost_attribution(session)

    assert attribution["trial_id"] == trial.id
    assert attribution["experiment_id"] == experiment.id
    assert attribution["billed_user_id"] == "user-abc"
    # attribution_org_id overrides the subject's org rather than discarding
    # the subject, which is exactly what it used to do.
    assert attribution["org_id"] == _ORG


@pytest.mark.asyncio
async def test_task_scope_resolves_task_id(session):
    task, _experiment, _trial = await _trial_fixture(session)

    block = _block(
        subject_type="task", subject_id=task.id, attribution_org_id=_ORG
    )
    attribution = await block._cost_attribution(session)

    assert attribution["task_id"] == task.id
    assert attribution["trial_id"] is None
    assert attribution["experiment_id"] is None


@pytest.mark.asyncio
async def test_experiment_scope_resolves_experiment_id(session):
    _task, experiment, _trial = await _trial_fixture(session)

    block = _block(
        subject_type="experiment",
        subject_id=experiment.id,
        attribution_org_id=_ORG,
    )
    attribution = await block._cost_attribution(session)

    assert attribution["experiment_id"] == experiment.id
    assert attribution["trial_id"] is None
    assert attribution["task_id"] is None


@pytest.mark.asyncio
async def test_cohort_block_still_resolves_to_null_subject(session):
    """A cohort spans many trials, so there is no single subject to charge.

    This is intentional, not an oversight -- keep it that way.
    """
    await _trial_fixture(session)

    block = _block(subject_type=None, subject_id=None, attribution_org_id=_ORG)
    attribution = await block._cost_attribution(session)

    assert attribution["trial_id"] is None
    assert attribution["task_id"] is None
    assert attribution["experiment_id"] is None
    assert attribution["org_id"] == _ORG


@pytest.mark.asyncio
async def test_unknown_subject_id_degrades_to_null_not_error(session):
    """A dangling subject id must not take down cost recording."""
    block = _block(
        subject_type="trial",
        subject_id="does-not-exist",
        attribution_org_id=_ORG,
    )
    attribution = await block._cost_attribution(session)

    assert attribution["trial_id"] is None
    assert attribution["org_id"] == _ORG
