"""`build_cohort_rollup`: models x categories over an experiment's stored
cohort comparisons.

The rollup is a READ path (see module docstring on `cohort_rollup.py`): a
task version without a fresh stored comparison is counted and named in
`coverage.missing`, never generated. Fixtures insert `analyzer_blocks` rows
directly so `_load_fresh_comparison` finds them without any generation.

Fixtures insert rows via `get_session()` and clean up afterward, mirroring
`test_cohort_rollup_resolution.py`; `backend/tests` has no shared `session`
fixture, so this file defines its own local one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from api.services.blocks.analyzer.cohort.cohort_comparison_block import SCHEMA_VERSION
from api.services.cohort_comparison import FAILURE_CLASS, SUCCESS_CLASS, cohort_hash
from api.services.cohort_rollup import build_cohort_rollup
from models import OrganizationModel
from oddish.blocks.analyzer.analyzer_block import AnalyzerType
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from oddish.db import (
    AnalyzerBlockModel,
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    get_session,
)
from oddish.db.models import JobStatus


# ---------------------------------------------------------------------------
# session fixture (local to this file -- backend/tests has no shared one)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session():
    async with get_session() as s:
        yield s


# ---------------------------------------------------------------------------
# Teardown helper
# ---------------------------------------------------------------------------


async def _cleanup(
    *,
    trial_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    experiment_ids: list[str] | None = None,
    org_ids: list[str] | None = None,
) -> None:
    async with get_session() as session:
        if task_ids:
            await session.execute(
                AnalyzerBlockModel.__table__.delete().where(
                    AnalyzerBlockModel.task_id.in_(task_ids)
                )
            )
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


# ---------------------------------------------------------------------------
# Fixture-building helpers
# ---------------------------------------------------------------------------


def _trial(
    *,
    trial_id: str,
    task_id: str,
    task_version_id: str,
    experiment_id: str,
    org_id: str,
    classification: str,
    model: str | None,
    agent: str = "claude-code",
) -> TrialModel:
    """A trial classified into a cohort side, with a citable summary component."""
    status = TrialStatus.SUCCESS if classification == SUCCESS_CLASS else TrialStatus.FAILED
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task_id,
        task_version_id=task_version_id,
        experiment_id=experiment_id,
        org_id=org_id,
        agent=agent,
        provider="anthropic",
        model=model,
        queue_key=f"queue-{trial_id}",
        status=status,
        analysis={"classification": classification},
        trajectory_summary={
            "components": [
                {
                    "trajectory_component": "debugging",
                    "step_ids": [1, 2],
                    "summary": f"summary-{trial_id}",
                }
            ]
        },
    )


def _comparison_block(
    *,
    task_id: str,
    task_version_id: str,
    cohort_hash_value: str,
    categories: list[dict],
) -> AnalyzerBlockModel:
    """A fresh, SUCCESS `cohort_comparison` block `_load_fresh_comparison` accepts."""
    return AnalyzerBlockModel(
        task_id=task_id,
        type=AnalyzerType.COHORT_COMPARISON.value,
        key_prefix="test-cohort-rollup",
        llm_client_type=LLMClientType.API.value,
        status=JobStatus.SUCCESS,
        output={"categories": categories},
        block_metadata={
            "schema_version": SCHEMA_VERSION,
            "cohort_hash": cohort_hash_value,
            "task_version_id": task_version_id,
        },
    )


def _evidence_category(name: str, *, successful_ids: list[str], failing_ids: list[str]) -> dict:
    return {
        "category": name,
        "successful": [
            {"evidence": [{"trial_id": tid} for tid in successful_ids]}
        ],
        "failing": [
            {"evidence": [{"trial_id": tid} for tid in failing_ids]}
        ],
    }


# ---------------------------------------------------------------------------
# Fixture: experiment_two_of_three_compared
# ---------------------------------------------------------------------------


@dataclass
class ExperimentTwoOfThreeCompared:
    experiment_id: str
    org_id: str
    task_ids: list[str] = field(default_factory=list)


@pytest_asyncio.fixture
async def experiment_two_of_three_compared():
    """Three task versions in one experiment; only two carry a fresh comparison."""
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_23c_{suffix}"
    experiment_id = f"exp_23c_{suffix}"
    task_ids: list[str] = []
    trial_ids: list[str] = []

    async with get_session() as session:
        session.add(
            OrganizationModel(id=org_id, name=f"23c Org {suffix}", slug=f"23c-org-{suffix}")
        )
        session.add(ExperimentModel(id=experiment_id, name=f"23c-exp-{suffix}", org_id=org_id))
        await session.flush()

        for i in range(3):
            task_id = f"task_23c_{suffix}_{i}"
            task_ids.append(task_id)
            session.add(
                TaskModel(
                    id=task_id,
                    name=f"23c-task-{suffix}-{i}",
                    user="test",
                    task_path="/tmp/fake",
                    org_id=org_id,
                )
            )
            await session.flush()
            version_id = f"{task_id}-v1"
            session.add(
                TaskVersionModel(
                    id=version_id, task_id=task_id, version=1, task_path="/tmp/fake"
                )
            )
            await session.flush()

            success_id = f"trial_23c_{suffix}_{i}_s"
            failure_id = f"trial_23c_{suffix}_{i}_f"
            trial_ids.extend([success_id, failure_id])
            session.add(
                _trial(
                    trial_id=success_id,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    classification=SUCCESS_CLASS,
                    model="claude-code",
                )
            )
            session.add(
                _trial(
                    trial_id=failure_id,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    classification=FAILURE_CLASS,
                    model="claude-code",
                )
            )
            await session.flush()

            # Only the first two task versions get a stored comparison; the
            # third is left uncompared.
            if i < 2:
                session.add(
                    _comparison_block(
                        task_id=task_id,
                        task_version_id=version_id,
                        cohort_hash_value=cohort_hash([success_id], [failure_id]),
                        categories=[],
                    )
                )

    yield ExperimentTwoOfThreeCompared(
        experiment_id=experiment_id, org_id=org_id, task_ids=list(task_ids)
    )

    await _cleanup(
        trial_ids=trial_ids,
        task_ids=task_ids,
        experiment_ids=[experiment_id],
        org_ids=[org_id],
    )


# ---------------------------------------------------------------------------
# Fixture: experiment_opus_and_grok
# ---------------------------------------------------------------------------


@dataclass
class ExperimentOpusAndGrok:
    experiment_id: str
    org_id: str


@pytest_asyncio.fixture
async def experiment_opus_and_grok():
    """One compared task version with two models at different baselines.

    opus: 8 success / 2 failure cohort (baseline .80); "debugging" cites 5 of
    the success trials and 1 of the failure trials (ratio .833).
    grok: 2 success / 6 failure cohort (baseline .25); "debugging" cites both
    success trials and 1 of the failure trials (ratio .667).

    A "behavior_discovery" category is also stored, to prove the matrix
    actually filters it out rather than merely never having received it.
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_og_{suffix}"
    experiment_id = f"exp_og_{suffix}"
    task_id = f"task_og_{suffix}"
    trial_ids: list[str] = []

    async with get_session() as session:
        session.add(
            OrganizationModel(id=org_id, name=f"OG Org {suffix}", slug=f"og-org-{suffix}")
        )
        session.add(ExperimentModel(id=experiment_id, name=f"og-exp-{suffix}", org_id=org_id))
        session.add(
            TaskModel(
                id=task_id,
                name=f"og-task-{suffix}",
                user="test",
                task_path="/tmp/fake",
                org_id=org_id,
            )
        )
        await session.flush()
        version_id = f"{task_id}-v1"
        session.add(
            TaskVersionModel(id=version_id, task_id=task_id, version=1, task_path="/tmp/fake")
        )
        await session.flush()

        opus_success = [f"trial_og_{suffix}_opus_s_{i}" for i in range(8)]
        opus_failure = [f"trial_og_{suffix}_opus_f_{i}" for i in range(2)]
        grok_success = [f"trial_og_{suffix}_grok_s_{i}" for i in range(2)]
        grok_failure = [f"trial_og_{suffix}_grok_f_{i}" for i in range(6)]

        for tid in opus_success:
            session.add(
                _trial(
                    trial_id=tid,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    classification=SUCCESS_CLASS,
                    model="opus",
                )
            )
        for tid in opus_failure:
            session.add(
                _trial(
                    trial_id=tid,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    classification=FAILURE_CLASS,
                    model="opus",
                )
            )
        for tid in grok_success:
            session.add(
                _trial(
                    trial_id=tid,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    classification=SUCCESS_CLASS,
                    model="grok",
                )
            )
        for tid in grok_failure:
            session.add(
                _trial(
                    trial_id=tid,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    classification=FAILURE_CLASS,
                    model="grok",
                )
            )
        trial_ids.extend(opus_success + opus_failure + grok_success + grok_failure)
        await session.flush()

        all_success = opus_success + grok_success
        all_failure = opus_failure + grok_failure
        categories = [
            _evidence_category(
                "debugging",
                successful_ids=opus_success[:5] + grok_success,
                failing_ids=opus_failure[:1] + grok_failure[:1],
            ),
            _evidence_category(
                "behavior_discovery",
                successful_ids=opus_success[:1],
                failing_ids=opus_failure[:1],
            ),
        ]
        session.add(
            _comparison_block(
                task_id=task_id,
                task_version_id=version_id,
                cohort_hash_value=cohort_hash(all_success, all_failure),
                categories=categories,
            )
        )

    yield ExperimentOpusAndGrok(experiment_id=experiment_id, org_id=org_id)

    await _cleanup(
        trial_ids=trial_ids,
        task_ids=[task_id],
        experiment_ids=[experiment_id],
        org_ids=[org_id],
    )


# ---------------------------------------------------------------------------
# Fixture: experiment_null_model
# ---------------------------------------------------------------------------


@dataclass
class ExperimentNullModel:
    experiment_id: str
    org_id: str


@pytest_asyncio.fixture
async def experiment_null_model():
    """A compared task version whose trials predate the `model` column."""
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_nm_{suffix}"
    experiment_id = f"exp_nm_{suffix}"
    task_id = f"task_nm_{suffix}"
    trial_ids: list[str] = []

    async with get_session() as session:
        session.add(
            OrganizationModel(id=org_id, name=f"NM Org {suffix}", slug=f"nm-org-{suffix}")
        )
        session.add(ExperimentModel(id=experiment_id, name=f"nm-exp-{suffix}", org_id=org_id))
        session.add(
            TaskModel(
                id=task_id,
                name=f"nm-task-{suffix}",
                user="test",
                task_path="/tmp/fake",
                org_id=org_id,
            )
        )
        await session.flush()
        version_id = f"{task_id}-v1"
        session.add(
            TaskVersionModel(id=version_id, task_id=task_id, version=1, task_path="/tmp/fake")
        )
        await session.flush()

        success_id = f"trial_nm_{suffix}_s"
        failure_id = f"trial_nm_{suffix}_f"
        trial_ids.extend([success_id, failure_id])
        session.add(
            _trial(
                trial_id=success_id,
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=experiment_id,
                org_id=org_id,
                classification=SUCCESS_CLASS,
                model=None,
                agent="claude-code",
            )
        )
        session.add(
            _trial(
                trial_id=failure_id,
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=experiment_id,
                org_id=org_id,
                classification=FAILURE_CLASS,
                model=None,
                agent="claude-code",
            )
        )
        await session.flush()

        session.add(
            _comparison_block(
                task_id=task_id,
                task_version_id=version_id,
                cohort_hash_value=cohort_hash([success_id], [failure_id]),
                categories=[],
            )
        )

    yield ExperimentNullModel(experiment_id=experiment_id, org_id=org_id)

    await _cleanup(
        trial_ids=trial_ids,
        task_ids=[task_id],
        experiment_ids=[experiment_id],
        org_ids=[org_id],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_comparisons_are_counted_not_averaged_over(
    session, experiment_two_of_three_compared
):
    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_two_of_three_compared.experiment_id,
        org_id=experiment_two_of_three_compared.org_id,
    )
    assert body["coverage"]["task_versions_total"] == 3
    assert body["coverage"]["task_versions_compared"] == 2
    assert len(body["coverage"]["missing"]) == 1
    assert body["coverage"]["missing"][0]["task_name"]


@pytest.mark.asyncio
async def test_build_never_generates(session, experiment_two_of_three_compared, monkeypatch):
    """The rollup is a READ path. A page view must not cost an LLM call.

    Patching ``get_or_generate_comparison`` on the ``cc`` module object only
    intercepts call sites written as ``cc.get_or_generate_comparison(...)``.
    This codebase's established style for calling it is a direct from-import
    (``backend/api/routers/tasks.py`` imports the name and calls it
    unqualified) -- that binds to the real function at ``cohort_rollup.py``'s
    own import time, before this monkeypatch ever runs, so the patch alone
    would not catch a generation call written that way. The row count is the
    side effect that has to hold regardless of which style production code
    uses: a real generation always persists a new ``analyzer_blocks`` row.
    The monkeypatch is kept as a cheap second line of defence.
    """
    import api.services.cohort_comparison as cc

    async def explode(*args, **kwargs):
        raise AssertionError("build_cohort_rollup generated a comparison")

    monkeypatch.setattr(cc, "get_or_generate_comparison", explode)

    task_ids = experiment_two_of_three_compared.task_ids
    count_stmt = (
        select(func.count())
        .select_from(AnalyzerBlockModel)
        .where(AnalyzerBlockModel.task_id.in_(task_ids))
    )
    before = (await session.execute(count_stmt)).scalar_one()

    await build_cohort_rollup(
        session,
        experiment_id=experiment_two_of_three_compared.experiment_id,
        org_id=experiment_two_of_three_compared.org_id,
    )

    after = (await session.execute(count_stmt)).scalar_one()
    assert after == before


@pytest.mark.asyncio
async def test_model_delta_is_against_that_models_own_baseline(
    session, experiment_opus_and_grok
):
    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_opus_and_grok.experiment_id,
        org_id=experiment_opus_and_grok.org_id,
    )
    debugging = next(c for c in body["categories"] if c["category"] == "debugging")
    by_model = {m["model"]: m for m in debugging["per_model"]}
    # opus: baseline .80, cited 5 success / 1 fail -> ratio .833
    assert by_model["opus"]["delta"] == pytest.approx(0.0333, abs=1e-4)
    # grok: baseline .25, cited 2 success / 1 fail -> ratio .667
    assert by_model["grok"]["delta"] == pytest.approx(0.4167, abs=1e-4)


@pytest.mark.asyncio
async def test_null_model_falls_back_to_agent(session, experiment_null_model):
    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_null_model.experiment_id,
        org_id=experiment_null_model.org_id,
    )
    assert [m["model"] for m in body["models"]] == ["claude-code"]


@pytest.mark.asyncio
async def test_discovery_is_absent_from_the_matrix(session, experiment_opus_and_grok):
    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_opus_and_grok.experiment_id,
        org_id=experiment_opus_and_grok.org_id,
    )
    assert "behavior_discovery" not in {c["category"] for c in body["categories"]}
