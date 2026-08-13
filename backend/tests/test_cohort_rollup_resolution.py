"""Guards for `resolve_rollup_versions`'s membership predicate.

A collection experiment gathers trials additively through `experiment_trials`
without changing their `experiment_id`; an ordinary experiment owns trials
only through that FK and has no `experiment_trials` rows at all. Both
directions must resolve, or the union predicate isn't doing its job -- see
`oddish.core.experiment_membership.trial_in_experiment`.

Fixtures insert rows directly via `get_session()` (mirroring
`test_experiment_trials_api.py`) and clean up afterward; `backend/tests` has
no shared `session` fixture.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest
import pytest_asyncio

from api.services.cohort_rollup import resolve_rollup_versions
from models import OrganizationModel
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    experiment_trials,
    get_session,
)


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


async def _make_task_version(session, *, task_id: str, org_id: str, suffix: str) -> str:
    session.add(
        TaskModel(
            id=task_id,
            name=f"task-{suffix}",
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
    return version_id


async def _add_task_version(session, *, task_id: str, version: int) -> str:
    version_id = f"{task_id}-v{version}"
    session.add(
        TaskVersionModel(
            id=version_id, task_id=task_id, version=version, task_path="/tmp/fake"
        )
    )
    await session.flush()
    return version_id


async def _gather(session, *, collection_id: str, trial_id: str) -> None:
    await session.execute(
        experiment_trials.insert().values(
            experiment_id=collection_id, trial_id=trial_id, deleted_at=None
        )
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class CollectionWithTrials:
    experiment_id: str
    org_id: str
    version_ids: set[str] = field(default_factory=set)


@pytest_asyncio.fixture
async def collection_with_trials():
    """A collection experiment gathering trials homed in a different experiment.

    Membership lives only in `experiment_trials`; the trials keep the home
    experiment's id.
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_cwt_{suffix}"
    home_id = f"exp_cwt_home_{suffix}"
    collection_id = f"exp_cwt_coll_{suffix}"
    task_ids: list[str] = []
    trial_ids: list[str] = []
    version_ids: set[str] = set()

    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id, name=f"CWT Org {suffix}", slug=f"cwt-org-{suffix}"
            )
        )
        session.add(ExperimentModel(id=home_id, name=f"cwt-home-{suffix}", org_id=org_id))
        session.add(
            ExperimentModel(
                id=collection_id,
                name=f"cwt-collection-{suffix}",
                org_id=org_id,
                is_collection=True,
            )
        )
        await session.flush()

        for i in range(2):
            task_id = f"task_cwt_{suffix}_{i}"
            task_ids.append(task_id)
            version_id = await _make_task_version(
                session, task_id=task_id, org_id=org_id, suffix=f"{suffix}-{i}"
            )
            version_ids.add(version_id)

            trial_id = f"trial_cwt_{suffix}_{i}"
            trial_ids.append(trial_id)
            session.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=home_id,
                    org_id=org_id,
                    agent="claude-code",
                    provider="anthropic",
                    model="anthropic/claude-sonnet-4-6",
                    queue_key=f"cwt-queue-{suffix}-{i}",
                    status=TrialStatus.SUCCESS,
                )
            )
            await session.flush()
            await _gather(session, collection_id=collection_id, trial_id=trial_id)

    yield CollectionWithTrials(
        experiment_id=collection_id, org_id=org_id, version_ids=version_ids
    )

    await _cleanup(
        trial_ids=trial_ids,
        task_ids=task_ids,
        experiment_ids=[home_id, collection_id],
        org_ids=[org_id],
    )


@dataclass
class PlainExperimentWithTrials:
    experiment_id: str
    org_id: str
    version_ids: set[str] = field(default_factory=set)


@pytest_asyncio.fixture
async def plain_experiment_with_trials():
    """An ordinary experiment: trials carry its id directly, no join rows."""
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_pe_{suffix}"
    experiment_id = f"exp_pe_{suffix}"
    task_ids: list[str] = []
    trial_ids: list[str] = []
    version_ids: set[str] = set()

    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id, name=f"PE Org {suffix}", slug=f"pe-org-{suffix}"
            )
        )
        session.add(
            ExperimentModel(id=experiment_id, name=f"pe-experiment-{suffix}", org_id=org_id)
        )
        await session.flush()

        for i in range(2):
            task_id = f"task_pe_{suffix}_{i}"
            task_ids.append(task_id)
            version_id = await _make_task_version(
                session, task_id=task_id, org_id=org_id, suffix=f"{suffix}-{i}"
            )
            version_ids.add(version_id)

            trial_id = f"trial_pe_{suffix}_{i}"
            trial_ids.append(trial_id)
            session.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task_id,
                    task_version_id=version_id,
                    experiment_id=experiment_id,
                    org_id=org_id,
                    agent="claude-code",
                    provider="anthropic",
                    model="anthropic/claude-sonnet-4-6",
                    queue_key=f"pe-queue-{suffix}-{i}",
                    status=TrialStatus.SUCCESS,
                )
            )
            # No experiment_trials row: membership is the FK alone.

    yield PlainExperimentWithTrials(
        experiment_id=experiment_id, org_id=org_id, version_ids=version_ids
    )

    await _cleanup(
        trial_ids=trial_ids,
        task_ids=task_ids,
        experiment_ids=[experiment_id],
        org_ids=[org_id],
    )


@dataclass
class CollectionWithProbeAndRetry:
    experiment_id: str
    org_id: str
    probe_version_id: str
    stale_superseded_version_id: str


@pytest_asyncio.fixture
async def collection_with_probe_and_retry():
    """A collection gathering a probe trial and a superseded/retry pair.

    Neither the probe nor the superseded original should contribute a
    version; only the live retry does. The superseded original is pinned to
    its own, different task version -- a stale version distinct from the
    retry's -- so filtering it is actually observable: if the
    superseded-trial filter were dropped, that stale version would leak into
    the result set on its own, not just be redundantly re-contributed by the
    live retry.
    """
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_cpr_{suffix}"
    home_id = f"exp_cpr_home_{suffix}"
    collection_id = f"exp_cpr_coll_{suffix}"
    task_ids: list[str] = []
    trial_ids: list[str] = []

    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id, name=f"CPR Org {suffix}", slug=f"cpr-org-{suffix}"
            )
        )
        session.add(ExperimentModel(id=home_id, name=f"cpr-home-{suffix}", org_id=org_id))
        session.add(
            ExperimentModel(
                id=collection_id,
                name=f"cpr-collection-{suffix}",
                org_id=org_id,
                is_collection=True,
            )
        )
        await session.flush()

        # A normal, contributing trial.
        normal_task_id = f"task_cpr_normal_{suffix}"
        task_ids.append(normal_task_id)
        normal_version_id = await _make_task_version(
            session, task_id=normal_task_id, org_id=org_id, suffix=f"{suffix}-normal"
        )
        normal_trial_id = f"trial_cpr_normal_{suffix}"
        trial_ids.append(normal_trial_id)
        session.add(
            TrialModel(
                id=normal_trial_id,
                name=normal_trial_id,
                task_id=normal_task_id,
                task_version_id=normal_version_id,
                experiment_id=home_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key=f"cpr-queue-normal-{suffix}",
                status=TrialStatus.SUCCESS,
            )
        )
        await session.flush()
        await _gather(session, collection_id=collection_id, trial_id=normal_trial_id)

        # A probe trial: must not contribute its version.
        probe_task_id = f"task_cpr_probe_{suffix}"
        task_ids.append(probe_task_id)
        probe_version_id = await _make_task_version(
            session, task_id=probe_task_id, org_id=org_id, suffix=f"{suffix}-probe"
        )
        probe_trial_id = f"trial_cpr_probe_{suffix}"
        trial_ids.append(probe_trial_id)
        session.add(
            TrialModel(
                id=probe_trial_id,
                name=probe_trial_id,
                task_id=probe_task_id,
                task_version_id=probe_version_id,
                experiment_id=home_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key=f"cpr-queue-probe-{suffix}",
                status=TrialStatus.SUCCESS,
                is_probe=True,
            )
        )
        await session.flush()
        await _gather(session, collection_id=collection_id, trial_id=probe_trial_id)

        # A superseded original (pinned to a stale version of its own) plus
        # its live retry (a newer version of the same task), both gathered.
        # The live retry is inserted first: superseded_by_trial_id is a real
        # FK to trials.id, so the target row must exist before it's pointed at.
        retry_task_id = f"task_cpr_retry_{suffix}"
        task_ids.append(retry_task_id)
        stale_version_id = await _make_task_version(
            session, task_id=retry_task_id, org_id=org_id, suffix=f"{suffix}-retry"
        )
        retry_version_id = await _add_task_version(
            session, task_id=retry_task_id, version=2
        )

        retry_new_id = f"trial_cpr_retry_new_{suffix}"
        retry_old_id = f"trial_cpr_retry_old_{suffix}"
        trial_ids.extend([retry_new_id, retry_old_id])
        session.add(
            TrialModel(
                id=retry_new_id,
                name=retry_new_id,
                task_id=retry_task_id,
                task_version_id=retry_version_id,
                experiment_id=home_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key=f"cpr-queue-retry-new-{suffix}",
                status=TrialStatus.SUCCESS,
            )
        )
        await session.flush()
        session.add(
            TrialModel(
                id=retry_old_id,
                name=retry_old_id,
                task_id=retry_task_id,
                task_version_id=stale_version_id,
                experiment_id=home_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key=f"cpr-queue-retry-old-{suffix}",
                status=TrialStatus.FAILED,
                superseded_by_trial_id=retry_new_id,
            )
        )
        await session.flush()
        await _gather(session, collection_id=collection_id, trial_id=retry_new_id)
        await _gather(session, collection_id=collection_id, trial_id=retry_old_id)

    yield CollectionWithProbeAndRetry(
        experiment_id=collection_id,
        org_id=org_id,
        probe_version_id=probe_version_id,
        stale_superseded_version_id=stale_version_id,
    )

    await _cleanup(
        trial_ids=trial_ids,
        task_ids=task_ids,
        experiment_ids=[home_id, collection_id],
        org_ids=[org_id],
    )


@dataclass
class ForeignCollection:
    experiment_id: str
    org_id: str


@pytest_asyncio.fixture
async def foreign_collection():
    """A collection owned by a real org, for querying with a mismatched org_id."""
    suffix = uuid.uuid4().hex[:8]
    org_id = f"org_fc_{suffix}"
    home_id = f"exp_fc_home_{suffix}"
    collection_id = f"exp_fc_coll_{suffix}"
    task_id = f"task_fc_{suffix}"
    trial_id = f"trial_fc_{suffix}"

    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id, name=f"FC Org {suffix}", slug=f"fc-org-{suffix}"
            )
        )
        session.add(ExperimentModel(id=home_id, name=f"fc-home-{suffix}", org_id=org_id))
        session.add(
            ExperimentModel(
                id=collection_id,
                name=f"fc-collection-{suffix}",
                org_id=org_id,
                is_collection=True,
            )
        )
        await session.flush()

        version_id = await _make_task_version(
            session, task_id=task_id, org_id=org_id, suffix=suffix
        )
        session.add(
            TrialModel(
                id=trial_id,
                name=trial_id,
                task_id=task_id,
                task_version_id=version_id,
                experiment_id=home_id,
                org_id=org_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key=f"fc-queue-{suffix}",
                status=TrialStatus.SUCCESS,
            )
        )
        await session.flush()
        await _gather(session, collection_id=collection_id, trial_id=trial_id)

    yield ForeignCollection(experiment_id=collection_id, org_id=org_id)

    await _cleanup(
        trial_ids=[trial_id],
        task_ids=[task_id],
        experiment_ids=[home_id, collection_id],
        org_ids=[org_id],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collection_membership_resolves_through_the_join(collection_with_trials):
    """A collection carries membership only in experiment_trials.

    Its trials keep the experiment_id of wherever they originally ran, so an
    FK-only filter returns an empty set silently -- the exact failure that made
    `oddish combine` a no-op on collections.
    """
    async with get_session() as session:
        versions = await resolve_rollup_versions(
            session,
            experiment_id=collection_with_trials.experiment_id,
            org_id=collection_with_trials.org_id,
        )
    assert {v.task_version_id for v in versions} == collection_with_trials.version_ids


@pytest.mark.asyncio
async def test_plain_experiment_membership_resolves_through_the_fk(
    plain_experiment_with_trials,
):
    """An ordinary experiment has no experiment_trials rows at all.

    The mirror of the case above: a join-only filter resolves it to nothing.
    Both tests must pass for the union predicate to be doing its job.
    """
    async with get_session() as session:
        versions = await resolve_rollup_versions(
            session,
            experiment_id=plain_experiment_with_trials.experiment_id,
            org_id=plain_experiment_with_trials.org_id,
        )
    assert {v.task_version_id for v in versions} == (
        plain_experiment_with_trials.version_ids
    )


@pytest.mark.asyncio
async def test_probe_and_superseded_trials_do_not_contribute_versions(
    collection_with_probe_and_retry,
):
    async with get_session() as session:
        versions = await resolve_rollup_versions(
            session,
            experiment_id=collection_with_probe_and_retry.experiment_id,
            org_id=collection_with_probe_and_retry.org_id,
        )
    contributed = {v.task_version_id for v in versions}
    assert collection_with_probe_and_retry.probe_version_id not in contributed
    assert collection_with_probe_and_retry.stale_superseded_version_id not in contributed


@pytest.mark.asyncio
async def test_another_orgs_experiment_resolves_empty(foreign_collection):
    async with get_session() as session:
        versions = await resolve_rollup_versions(
            session,
            experiment_id=foreign_collection.experiment_id,
            org_id="org_not_the_owner",
        )
    assert versions == []
