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
    """Three task versions in one experiment; only two carry a fresh comparison.

    The two compared versions are 1 success / 1 failure each, so the single
    model's baseline over the included cohort is 0.5. The uncompared third is
    deliberately lopsided -- 6 successes, no failures -- so that a version
    leaking past the ``continue`` would move the baseline to 8/10 = 0.8. The
    asymmetry is what makes ``test_missing_comparisons_are_counted_not_averaged_over``
    able to fail; with 1/1 everywhere the assertion held either way.
    """
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

            compared = i < 2
            success_ids = (
                [f"trial_23c_{suffix}_{i}_s"]
                if compared
                else [f"trial_23c_{suffix}_{i}_s{k}" for k in range(6)]
            )
            failure_ids = [f"trial_23c_{suffix}_{i}_f"] if compared else []
            trial_ids.extend(success_ids + failure_ids)
            for tid, cls in [(t, SUCCESS_CLASS) for t in success_ids] + [
                (t, FAILURE_CLASS) for t in failure_ids
            ]:
                session.add(
                    _trial(
                        trial_id=tid,
                        task_id=task_id,
                        task_version_id=version_id,
                        experiment_id=experiment_id,
                        org_id=org_id,
                        classification=cls,
                        model="claude-code",
                    )
                )
            await session.flush()

            # Only the first two task versions get a stored comparison; the
            # third is left uncompared.
            if compared:
                session.add(
                    _comparison_block(
                        task_id=task_id,
                        task_version_id=version_id,
                        cohort_hash_value=cohort_hash(success_ids, failure_ids),
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
# Fixture: experiment_below_cohort_gate
# ---------------------------------------------------------------------------


@dataclass
class ExperimentBelowCohortGate:
    experiment_id: str
    org_id: str


@pytest_asyncio.fixture
async def experiment_below_cohort_gate():
    """One uncompared task version with two classified trials.

    ``MIN_COHORT`` is 3, so ``get_or_generate_comparison`` would decline this
    version: it can never be compared as it stands, and offering it to the
    reader as a link to go and generate one is an invitation to a refusal.
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_bg_{suffix}"
    experiment_id = f"exp_bg_{suffix}"
    task_id = f"task_bg_{suffix}"

    async with get_session() as session:
        session.add(
            OrganizationModel(id=org_id, name=f"BG Org {suffix}", slug=f"bg-org-{suffix}")
        )
        session.add(ExperimentModel(id=experiment_id, name=f"bg-exp-{suffix}", org_id=org_id))
        session.add(
            TaskModel(
                id=task_id,
                name=f"bg-task-{suffix}",
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

        trial_ids = [f"trial_bg_{suffix}_s", f"trial_bg_{suffix}_f"]
        for tid, cls in zip(trial_ids, (SUCCESS_CLASS, FAILURE_CLASS)):
            session.add(
                _trial(
                    trial_id=tid,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    classification=cls,
                    model="claude-code",
                )
            )

    yield ExperimentBelowCohortGate(experiment_id=experiment_id, org_id=org_id)

    await _cleanup(
        trial_ids=trial_ids,
        task_ids=[task_id],
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
            # The same opus trials again, so the sum of per-category `n` (12)
            # diverges from the distinct runs behind them (6).
            _evidence_category(
                "planning",
                successful_ids=opus_success[:5],
                failing_ids=opus_failure[:1],
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
# Fixture: experiment_one_sided_and_mixed
# ---------------------------------------------------------------------------


@dataclass
class ExperimentOneSidedAndMixed:
    experiment_id: str
    org_id: str


@pytest_asyncio.fixture
async def experiment_one_sided_and_mixed():
    """One compared version; `solo` never succeeds, `mixed` does both.

    solo: 0 success / 4 failure -- baseline 0, and every trial it could
    possibly be cited for is a failure, so its ratio is 0 on every axis and
    subtracting the baseline gives a confident 0.00 across the board.
    mixed: 2 success / 2 failure -- baseline .5, and "debugging" cites 2 of
    its successes and 1 failure, so it keeps a real +.1667.
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_os_{suffix}"
    experiment_id = f"exp_os_{suffix}"
    task_id = f"task_os_{suffix}"
    trial_ids: list[str] = []

    async with get_session() as session:
        session.add(
            OrganizationModel(id=org_id, name=f"OS Org {suffix}", slug=f"os-org-{suffix}")
        )
        session.add(ExperimentModel(id=experiment_id, name=f"os-exp-{suffix}", org_id=org_id))
        session.add(
            TaskModel(
                id=task_id,
                name=f"os-task-{suffix}",
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

        solo_failure = [f"trial_os_{suffix}_solo_f{i}" for i in range(4)]
        mixed_success = [f"trial_os_{suffix}_mixed_s{i}" for i in range(2)]
        mixed_failure = [f"trial_os_{suffix}_mixed_f{i}" for i in range(2)]
        rows = (
            [(t, FAILURE_CLASS, "solo") for t in solo_failure]
            + [(t, SUCCESS_CLASS, "mixed") for t in mixed_success]
            + [(t, FAILURE_CLASS, "mixed") for t in mixed_failure]
        )
        for tid, cls, model in rows:
            trial_ids.append(tid)
            session.add(
                _trial(
                    trial_id=tid,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    classification=cls,
                    model=model,
                )
            )
        await session.flush()

        session.add(
            _comparison_block(
                task_id=task_id,
                task_version_id=version_id,
                cohort_hash_value=cohort_hash(
                    [t for t, cls, _ in rows if cls == SUCCESS_CLASS],
                    [t for t, cls, _ in rows if cls == FAILURE_CLASS],
                ),
                categories=[
                    _evidence_category(
                        "debugging",
                        successful_ids=mixed_success,
                        failing_ids=solo_failure[:3] + mixed_failure[:1],
                    )
                ],
            )
        )

    yield ExperimentOneSidedAndMixed(experiment_id=experiment_id, org_id=org_id)

    await _cleanup(
        trial_ids=trial_ids,
        task_ids=[task_id],
        experiment_ids=[experiment_id],
        org_ids=[org_id],
    )


# ---------------------------------------------------------------------------
# Fixture: experiment_sharing_a_task_version
# ---------------------------------------------------------------------------


@dataclass
class ExperimentSharingATaskVersion:
    experiment_id: str
    org_id: str


@pytest_asyncio.fixture
async def experiment_sharing_a_task_version():
    """One task version run by two experiments; we roll up only one of them.

    A task version belongs to a task, not to whoever ran it, so
    ``resolve_cohorts`` returns every classified trial on it regardless of
    experiment. Ours owns one opus success and one opus failure -- baseline
    .5. The neighbouring experiment owns four more opus successes and a grok
    pair, all on the same version, so a rollup that took the cohort at face
    value would read opus at 5/6 = .833 and invent a grok row entirely.

    The stored comparison is hashed over *all* eight trials, as a real one
    would be: the comparison was generated for the task version, and scoping
    the cohort before hashing would leave nothing that ever matches.
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_sh_{suffix}"
    experiment_id = f"exp_sh_mine_{suffix}"
    neighbour_id = f"exp_sh_theirs_{suffix}"
    task_id = f"task_sh_{suffix}"
    trial_ids: list[str] = []

    async with get_session() as session:
        session.add(
            OrganizationModel(id=org_id, name=f"SH Org {suffix}", slug=f"sh-org-{suffix}")
        )
        session.add(ExperimentModel(id=experiment_id, name=f"sh-mine-{suffix}", org_id=org_id))
        session.add(
            ExperimentModel(id=neighbour_id, name=f"sh-theirs-{suffix}", org_id=org_id)
        )
        session.add(
            TaskModel(
                id=task_id,
                name=f"sh-task-{suffix}",
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

        mine_success = f"trial_sh_{suffix}_mine_s"
        mine_failure = f"trial_sh_{suffix}_mine_f"
        theirs_opus = [f"trial_sh_{suffix}_their_opus_s{i}" for i in range(4)]
        theirs_grok_s = f"trial_sh_{suffix}_their_grok_s"
        theirs_grok_f = f"trial_sh_{suffix}_their_grok_f"

        rows = [
            (mine_success, experiment_id, SUCCESS_CLASS, "opus"),
            (mine_failure, experiment_id, FAILURE_CLASS, "opus"),
            *[(t, neighbour_id, SUCCESS_CLASS, "opus") for t in theirs_opus],
            (theirs_grok_s, neighbour_id, SUCCESS_CLASS, "grok"),
            (theirs_grok_f, neighbour_id, FAILURE_CLASS, "grok"),
        ]
        for tid, owner, cls, model in rows:
            trial_ids.append(tid)
            session.add(
                _trial(
                    trial_id=tid,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=owner,
                    org_id=org_id,
                    classification=cls,
                    model=model,
                )
            )
        await session.flush()

        session.add(
            _comparison_block(
                task_id=task_id,
                task_version_id=version_id,
                cohort_hash_value=cohort_hash(
                    [t for t, _, cls, _ in rows if cls == SUCCESS_CLASS],
                    [t for t, _, cls, _ in rows if cls == FAILURE_CLASS],
                ),
                categories=[
                    # One of ours cited on each side, plus two of theirs on the
                    # success side. Scoped, opus reads 1 success / 1 failure.
                    _evidence_category(
                        "debugging",
                        successful_ids=[mine_success, theirs_opus[0], theirs_grok_s],
                        failing_ids=[mine_failure],
                    )
                ],
            )
        )

    yield ExperimentSharingATaskVersion(experiment_id=experiment_id, org_id=org_id)

    await _cleanup(
        trial_ids=trial_ids,
        task_ids=[task_id],
        experiment_ids=[experiment_id, neighbour_id],
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
    # The property the coverage counts exist to protect: an uncompared version
    # contributes nothing to any model's baseline. The two compared versions
    # are 1 success / 1 failure each; the uncompared third is 6 successes and
    # no failures, so leaking it in would read 8/10 = 0.8 here.
    assert body["models"][0]["baseline"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_uncompared_versions_skip_the_cohort_resolution(
    session, experiment_two_of_three_compared, monkeypatch
):
    """Only versions a stored comparison names pay for ``resolve_cohorts``.

    ``resolve_cohorts`` selects the ``trajectory_summary`` JSONB of every
    classified trial. Running it for a version that was never compared streams
    those blobs to throw them away, once per version, on every page view.
    """
    import api.services.cohort_rollup as cr

    seen: list[str] = []
    real = cr.resolve_cohorts

    async def counting(session_, task_version_id):
        seen.append(task_version_id)
        return await real(session_, task_version_id)

    monkeypatch.setattr(cr, "resolve_cohorts", counting)

    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_two_of_three_compared.experiment_id,
        org_id=experiment_two_of_three_compared.org_id,
    )
    assert body["coverage"]["task_versions_total"] == 3
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_missing_rows_carry_the_version_they_name(
    session, experiment_two_of_three_compared
):
    """A coverage row has to address the version it just named.

    The link the coverage line builds is the only way a reader acts on this
    list, and the task page resolves `?version=` against ids. With only the
    display number the link drops the param and opens on the task's current
    version -- routinely a later one than the row named, and sometimes one
    that is compared, so the page contradicts the sentence that linked it.
    """
    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_two_of_three_compared.experiment_id,
        org_id=experiment_two_of_three_compared.org_id,
    )
    missing = body["coverage"]["missing"]
    assert missing
    for row in missing:
        assert row["task_version_id"]
        # The id addresses the same version the row displays.
        assert row["task_version_id"].endswith(f"-v{row['version']}")
        assert row["task_version_id"].startswith(row["task_id"])


@pytest.mark.asyncio
async def test_missing_names_why_it_cannot_be_compared(
    session, experiment_two_of_three_compared, experiment_below_cohort_gate
):
    compared_gate = await build_cohort_rollup(
        session,
        experiment_id=experiment_two_of_three_compared.experiment_id,
        org_id=experiment_two_of_three_compared.org_id,
    )
    # Six classified successes: the gate would let this one be generated.
    assert [m["reason"] for m in compared_gate["coverage"]["missing"]] == [
        "not_compared"
    ]

    gated = await build_cohort_rollup(
        session,
        experiment_id=experiment_below_cohort_gate.experiment_id,
        org_id=experiment_below_cohort_gate.org_id,
    )
    # One success and one failure, against MIN_COHORT of 3.
    assert [m["reason"] for m in gated["coverage"]["missing"]] == [
        "below_cohort_gate"
    ]


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
async def test_cited_runs_counts_a_trial_once_across_categories(
    session, experiment_opus_and_grok
):
    """``cited_runs`` is the union of a model's cited trials, not a column sum.

    The radar gates on it, so a sum would let one trial cited in six categories
    pass a six-trial threshold and draw a fully confident shape.
    """
    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_opus_and_grok.experiment_id,
        org_id=experiment_opus_and_grok.org_id,
    )
    by_model = {m["model"]: m for m in body["models"]}
    per_category_total = {
        m["model"]: sum(
            cell["n"]
            for cat in body["categories"]
            for cell in cat["per_model"]
            if cell["model"] == m["model"]
        )
        for m in body["models"]
    }
    # opus is cited in two categories over the same six trials.
    assert by_model["opus"]["cited_runs"] == 6
    assert per_category_total["opus"] == 12
    assert by_model["grok"]["cited_runs"] == 3


@pytest.mark.asyncio
async def test_discovery_is_absent_from_the_matrix(session, experiment_opus_and_grok):
    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_opus_and_grok.experiment_id,
        org_id=experiment_opus_and_grok.org_id,
    )
    assert "behavior_discovery" not in {c["category"] for c in body["categories"]}


@pytest.mark.asyncio
async def test_a_one_sided_models_axes_are_absent_not_average(
    session, experiment_one_sided_and_mixed
):
    """A model with only one cohort side reports no delta, but keeps its ratio.

    Real data made this concrete: in the OTS rollup a gemini variant held a
    0-success / 9-failure cohort and scored exactly 0.0 on all five axes it
    had citations for, drawing a closed shape on the baseline ring -- a model
    that never once succeeded, rendered as average at everything.

    The mixed model in the same fixture is what keeps the guard from being a
    blanket: its deltas have to survive.
    """
    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_one_sided_and_mixed.experiment_id,
        org_id=experiment_one_sided_and_mixed.org_id,
    )
    by_model = {m["model"]: m for m in body["models"]}
    assert (by_model["solo"]["cohort_success"], by_model["solo"]["cohort_failure"]) == (
        0,
        4,
    )
    assert (
        by_model["mixed"]["cohort_success"],
        by_model["mixed"]["cohort_failure"],
    ) == (2, 2)

    debugging = next(c for c in body["categories"] if c["category"] == "debugging")
    cells = {c["model"]: c for c in debugging["per_model"]}
    # The citations are real and still counted; only the comparison is void.
    assert cells["solo"]["n"] == 3
    assert cells["solo"]["ratio"] == 0.0
    assert cells["solo"]["delta"] is None
    # The two-sided model is untouched: baseline .5, cited 2 of 3 -> +.1667
    assert cells["mixed"]["delta"] == pytest.approx(0.1667, abs=1e-4)


@pytest.mark.asyncio
async def test_a_neighbours_trials_do_not_enter_the_baseline(
    session, experiment_sharing_a_task_version
):
    """Version discovery is scoped by membership; the cohort must be too.

    ``resolve_cohorts`` takes a task version, so it hands back the neighbouring
    experiment's runs alongside ours. Ours is one opus success and one opus
    failure; the neighbour adds four opus successes on the same version, which
    would drag the baseline the radar measures every axis against from .5 to
    .833 -- the model's record on the task, not in this experiment.
    """
    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_sharing_a_task_version.experiment_id,
        org_id=experiment_sharing_a_task_version.org_id,
    )
    opus = next(m for m in body["models"] if m["model"] == "opus")
    assert (opus["cohort_success"], opus["cohort_failure"]) == (1, 1)
    assert opus["baseline"] == pytest.approx(0.5)
    assert body["pooled_baseline"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_a_neighbours_model_and_citations_do_not_enter_the_rollup(
    session, experiment_sharing_a_task_version
):
    """grok never ran in this experiment, so it cannot have an axis in it.

    The citation filter is the same one: the stored comparison cites two of the
    neighbour's trials on the success side, and counting them would put opus at
    2 cited successes off runs this experiment does not own.
    """
    body = await build_cohort_rollup(
        session,
        experiment_id=experiment_sharing_a_task_version.experiment_id,
        org_id=experiment_sharing_a_task_version.org_id,
    )
    assert [m["model"] for m in body["models"]] == ["opus"]

    debugging = next(c for c in body["categories"] if c["category"] == "debugging")
    assert [cell["model"] for cell in debugging["per_model"]] == ["opus"]
    opus_cell = debugging["per_model"][0]
    assert (opus_cell["cited_success"], opus_cell["cited_failure"]) == (1, 1)
    assert debugging["pooled"]["n"] == 2
