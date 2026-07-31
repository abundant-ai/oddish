"""QA/analysis spend rolled up per trial, task, and experiment.

Attribution on ``analysis_costs`` is asymmetric: ``trial_classifier`` rows set
``trial_id`` and the trial's HOME ``experiment_id`` but never ``task_id``;
task-level QA sets only ``task_id``. So task and experiment rollups resolve
through ``trials`` and UNION the directly-attributed rows, rather than trusting
a single scope column.

Filter parity with ``experiment_cost``: soft-deleted trials still count (the
money was spent), soft-deleted MEMBERSHIP rows do not (the trial left the
collection).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import insert

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.qa_cost import (  # noqa: E402
    get_experiment_qa_cost_totals,
    get_task_qa_costs,
    get_trial_qa_costs,
)
from oddish.core.endpoints.tasks_query import list_experiment_slim_tasks  # noqa: E402
from oddish.core.endpoints.trials import (  # noqa: E402
    get_trial_by_index_core,
    get_trial_response_for_org_core,
)
from oddish.core.sharing.helpers import list_experiment_trials_for_org  # noqa: E402
from oddish.db.models import (  # noqa: E402
    AnalysisCostModel,
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    experiment_trials,
    generate_id,
    task_experiments,
    utcnow,
)

_ORG = f"qacost-org-{uuid.uuid4().hex[:8]}"


async def _fixture(session, slug: str) -> tuple[TaskModel, ExperimentModel]:
    task = TaskModel(
        name=f"qacost-task-{slug}-{_ORG}",
        org_id=_ORG,
        user="tester",
        task_path=f"s3://tasks/qacost-{slug}",
    )
    session.add(task)
    await session.flush()

    experiment = ExperimentModel(
        name=f"qacost-exp-{slug}-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add(experiment)
    await session.flush()
    return task, experiment


def _trial(task, experiment, *, deleted_at=None) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
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
        created_at=utcnow(),
        deleted_at=deleted_at,
    )


def _qa_row(
    *,
    cost_usd: float,
    trial_id: str | None = None,
    task_id: str | None = None,
    experiment_id: str | None = None,
    job_kind: str = "trial_classifier",
    cost_source: str = "estimated",
    deleted_at=None,
) -> AnalysisCostModel:
    return AnalysisCostModel(
        job_kind=job_kind,
        trial_id=trial_id,
        task_id=task_id,
        experiment_id=experiment_id,
        org_id=_ORG,
        model="claude-haiku-4-5",
        cost_usd=cost_usd,
        cost_source=cost_source,
        deleted_at=deleted_at,
    )


@pytest.mark.asyncio
async def test_trial_scope_sums_per_trial(session):
    task, experiment = await _fixture(session, "trialscope")
    a, b = _trial(task, experiment), _trial(task, experiment)
    session.add_all([a, b])
    await session.flush()

    session.add_all(
        [
            _qa_row(cost_usd=0.004, trial_id=a.id),
            _qa_row(cost_usd=0.006, trial_id=a.id),  # a re-classification
            _qa_row(cost_usd=0.002, trial_id=b.id),
            _qa_row(cost_usd=99.0, trial_id=a.id, deleted_at=utcnow()),  # voided
        ]
    )
    await session.flush()

    costs = await get_trial_qa_costs(session, trial_ids=[a.id, b.id], org_id=_ORG)

    assert costs[a.id] == pytest.approx(0.010)
    assert costs[b.id] == pytest.approx(0.002)


@pytest.mark.asyncio
async def test_trial_scope_omits_trials_with_no_qa(session):
    """Absent, not zero -- the UI renders nothing rather than '+$0.00 QA'."""
    task, experiment = await _fixture(session, "noqa")
    trial = _trial(task, experiment)
    session.add(trial)
    await session.flush()

    costs = await get_trial_qa_costs(session, trial_ids=[trial.id], org_id=_ORG)

    assert trial.id not in costs


@pytest.mark.asyncio
async def test_task_scope_joins_through_trials(session):
    """The core case: ``trial_classifier`` rows never set ``task_id``.

    A rollup reading ``analysis_costs.task_id`` directly would report $0 here.
    """
    task, experiment = await _fixture(session, "taskjoin")
    a, b = _trial(task, experiment), _trial(task, experiment)
    session.add_all([a, b])
    await session.flush()

    session.add_all(
        [
            _qa_row(cost_usd=0.03, trial_id=a.id),
            _qa_row(cost_usd=0.05, trial_id=b.id),
        ]
    )
    await session.flush()

    totals = await get_task_qa_costs(session, task_ids=[task.id], org_id=_ORG)

    assert totals[task.id].qa_cost_usd == pytest.approx(0.08)
    assert totals[task.id].qa_job_count == 2


@pytest.mark.asyncio
async def test_task_scope_unions_task_level_qa_without_double_counting(session):
    """Task-level QA sets ``task_id`` and no ``trial_id``; both must count once."""
    task, experiment = await _fixture(session, "taskunion")
    trial = _trial(task, experiment)
    session.add(trial)
    await session.flush()

    session.add_all(
        [
            _qa_row(cost_usd=0.03, trial_id=trial.id),
            _qa_row(cost_usd=0.07, task_id=task.id, job_kind="CUSTOM_QA"),
            _qa_row(
                cost_usd=99.0,
                task_id=task.id,
                job_kind="CUSTOM_QA",
                deleted_at=utcnow(),
            ),  # voided
        ]
    )
    await session.flush()

    totals = await get_task_qa_costs(session, task_ids=[task.id], org_id=_ORG)

    assert totals[task.id].qa_cost_usd == pytest.approx(0.10)
    assert totals[task.id].qa_job_count == 2


@pytest.mark.asyncio
async def test_row_with_both_trial_and_task_id_counts_once(session):
    """After the Task 1 fix a row can carry both. It is one job, one charge."""
    task, experiment = await _fixture(session, "bothids")
    trial = _trial(task, experiment)
    session.add(trial)
    await session.flush()

    session.add(_qa_row(cost_usd=0.09, trial_id=trial.id, task_id=task.id))
    await session.flush()

    totals = await get_task_qa_costs(session, task_ids=[task.id], org_id=_ORG)

    assert totals[task.id].qa_cost_usd == pytest.approx(0.09)
    assert totals[task.id].qa_job_count == 1


@pytest.mark.asyncio
async def test_task_scope_counts_a_soft_deleted_trials_qa(session):
    """Same filter parity the experiment scope gets: the trial is gone from
    the grid, the money is not. The join to ``trials`` has to opt out of the
    soft-delete auto-filter or this silently under-reports."""
    task, experiment = await _fixture(session, "tasksoftdel")
    trial = _trial(task, experiment, deleted_at=utcnow())
    session.add(trial)
    await session.flush()

    session.add(_qa_row(cost_usd=0.05, trial_id=trial.id))
    await session.flush()

    totals = await get_task_qa_costs(session, task_ids=[task.id], org_id=_ORG)

    assert totals[task.id].qa_cost_usd == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_trial_scope_pairs_match_the_cards_agent_cost_population(session):
    """The task-browse card prices only current-version, non-probe,
    non-superseded, non-combine-copy, live trials. ``trial_scope_pairs`` holds
    the per-trial QA branch to that same population so the card's two figures
    describe the same trials, while task-level QA (no trial) still counts."""
    task, experiment = await _fixture(session, "cardscope")
    v_current = TaskVersionModel(
        id=f"{task.id}-v2", task_id=task.id, version=2, task_path=task.task_path
    )
    v_old = TaskVersionModel(
        id=f"{task.id}-v1", task_id=task.id, version=1, task_path=task.task_path
    )
    session.add_all([v_current, v_old])
    await session.flush()

    cur = _trial(task, experiment)
    cur.task_version_id = v_current.id

    superseded = _trial(task, experiment)
    superseded.task_version_id = v_current.id
    superseded.superseded_by_trial_id = cur.id

    probe = _trial(task, experiment)
    probe.task_version_id = v_current.id
    probe.is_probe = True

    combine_copy = _trial(task, experiment)
    combine_copy.task_version_id = v_current.id
    combine_copy.idempotency_key = "combine:abc"

    old_version = _trial(task, experiment)
    old_version.task_version_id = v_old.id

    soft_deleted = _trial(task, experiment, deleted_at=utcnow())
    soft_deleted.task_version_id = v_current.id

    session.add_all(
        [cur, superseded, probe, combine_copy, old_version, soft_deleted]
    )
    await session.flush()

    session.add_all(
        [
            _qa_row(cost_usd=0.01, trial_id=cur.id),
            _qa_row(cost_usd=0.02, trial_id=superseded.id),
            _qa_row(cost_usd=0.04, trial_id=probe.id),
            _qa_row(cost_usd=0.08, trial_id=combine_copy.id),
            _qa_row(cost_usd=0.16, trial_id=old_version.id),
            _qa_row(cost_usd=0.32, trial_id=soft_deleted.id),
            _qa_row(cost_usd=0.005, task_id=task.id, job_kind="CUSTOM_QA"),
        ]
    )
    await session.flush()

    scoped = await get_task_qa_costs(
        session,
        task_ids=[task.id],
        org_id=_ORG,
        trial_scope_pairs=[(task.id, v_current.id)],
    )
    # Current-version trial (0.01) + task-level direct QA (0.005) only.
    assert scoped[task.id].qa_cost_usd == pytest.approx(0.015)

    # Unscoped (task detail / experiment views) still counts every row.
    broad = await get_task_qa_costs(session, task_ids=[task.id], org_id=_ORG)
    assert broad[task.id].qa_cost_usd == pytest.approx(0.635)


@pytest.mark.asyncio
async def test_task_scope_returns_only_the_requested_tasks(session):
    """The returned dict is keyed by ids the caller asked for, never others.

    A row can name two tasks at once: ``analyzer_block``'s trial path copies
    the block's own ``task_id`` onto a trial-subject row, so the row's
    ``task_id`` and its trial's task need not agree. Folding the two paths into
    one ``COALESCE(analysis_costs.task_id, trials.task_id)`` charges whichever
    won the coalesce -- here handing back a task the caller never asked about
    and $0 for the one it did.
    """
    task_a, experiment = await _fixture(session, "leak-a")
    task_b, _experiment_b = await _fixture(session, "leak-b")
    trial = _trial(task_a, experiment)
    session.add(trial)
    await session.flush()

    session.add(_qa_row(cost_usd=0.11, trial_id=trial.id, task_id=task_b.id))
    await session.flush()

    totals = await get_task_qa_costs(session, task_ids=[task_a.id], org_id=_ORG)

    assert set(totals) == {task_a.id}
    assert totals[task_a.id].qa_cost_usd == pytest.approx(0.11)


@pytest.mark.asyncio
async def test_experiment_scope_counts_gathered_trials_but_not_as_owned(session):
    """``qa_cost_usd`` covers member trials; ``owned_qa_cost_usd`` only homed
    ones -- mirroring the agent-cost tile's two scopes."""
    task, home = await _fixture(session, "gathered-home")
    _task2, collection = await _fixture(session, "gathered-coll")

    homed = _trial(task, collection)
    gathered = _trial(task, home)
    unlinked = _trial(task, home)
    session.add_all([homed, gathered, unlinked])
    await session.flush()

    await session.execute(
        insert(experiment_trials).values(
            [
                {
                    "experiment_id": collection.id,
                    "trial_id": gathered.id,
                    "deleted_at": None,
                },
                # soft-deleted membership: no longer a member, cost excluded
                {
                    "experiment_id": collection.id,
                    "trial_id": unlinked.id,
                    "deleted_at": utcnow(),
                },
            ]
        )
    )
    session.add_all(
        [
            _qa_row(cost_usd=0.02, trial_id=homed.id),
            _qa_row(cost_usd=0.05, trial_id=gathered.id),
            _qa_row(cost_usd=99.0, trial_id=unlinked.id),
        ]
    )
    await session.flush()

    totals = await get_experiment_qa_cost_totals(
        session, experiment_id=collection.id, org_id=_ORG
    )

    assert totals.qa_cost_usd == pytest.approx(0.07)
    assert totals.owned_qa_cost_usd == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_owned_follows_the_trial_home_not_the_row_label(session):
    """When a row has a trial, its trial's home decides ``owned`` -- the row's
    own ``experiment_id`` does not.

    ``owned_*`` is the number that stays additive across experiments, so a row
    naming this experiment while its trial is homed elsewhere must not be
    charged here as owned: the home experiment already reports it. Only
    trial-less rows fall back to the row's label.
    """
    task, home = await _fixture(session, "ownedlabel-home")
    _task2, other = await _fixture(session, "ownedlabel-other")

    trial = _trial(task, home)
    session.add(trial)
    await session.flush()

    session.add_all(
        [
            _qa_row(cost_usd=0.03, trial_id=trial.id, experiment_id=other.id),
            _qa_row(cost_usd=0.06, experiment_id=other.id, job_kind="CUSTOM_QA"),
        ]
    )
    await session.flush()

    totals = await get_experiment_qa_cost_totals(
        session, experiment_id=other.id, org_id=_ORG
    )

    assert totals.qa_cost_usd == pytest.approx(0.09)
    assert totals.owned_qa_cost_usd == pytest.approx(0.06)
    assert totals.owned_qa_job_count == 1


@pytest.mark.asyncio
async def test_soft_deleted_trial_still_counts(session):
    """Deleting a trial removes it from the view; it does not refund it."""
    task, experiment = await _fixture(session, "softdel")
    trial = _trial(task, experiment, deleted_at=utcnow())
    session.add(trial)
    await session.flush()

    session.add(_qa_row(cost_usd=0.04, trial_id=trial.id))
    await session.flush()

    totals = await get_experiment_qa_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.qa_cost_usd == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_soft_deleted_analysis_cost_row_is_excluded(session):
    task, experiment = await _fixture(session, "softdelcost")
    trial = _trial(task, experiment)
    session.add(trial)
    await session.flush()

    session.add_all(
        [
            _qa_row(cost_usd=0.04, trial_id=trial.id),
            AnalysisCostModel(
                job_kind="trial_classifier",
                trial_id=trial.id,
                org_id=_ORG,
                model="claude-haiku-4-5",
                cost_usd=99.0,
                cost_source="estimated",
                deleted_at=utcnow(),
            ),
        ]
    )
    await session.flush()

    totals = await get_experiment_qa_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.qa_cost_usd == pytest.approx(0.04)


@pytest.mark.asyncio
async def test_cost_source_flags_track_native_and_estimated(session):
    task, experiment = await _fixture(session, "sources")
    trial = _trial(task, experiment)
    session.add(trial)
    await session.flush()

    session.add_all(
        [
            _qa_row(cost_usd=0.01, trial_id=trial.id, cost_source="native"),
            _qa_row(cost_usd=0.02, trial_id=trial.id, cost_source="estimated"),
        ]
    )
    await session.flush()

    totals = await get_experiment_qa_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.qa_has_native is True
    assert totals.qa_has_estimated is True


@pytest.mark.asyncio
async def test_empty_id_list_returns_empty_without_querying(session):
    assert await get_trial_qa_costs(session, trial_ids=[], org_id=_ORG) == {}
    assert await get_task_qa_costs(session, task_ids=[], org_id=_ORG) == {}


@pytest.mark.asyncio
async def test_trial_costs_batched_for_a_page_of_ids(session):
    """One query per page of trials, not one per trial.

    The paginated trial list must not gain an N+1; this pins the batch API
    that the serializer is required to use.
    """
    task, experiment = await _fixture(session, "batch")
    trials = [_trial(task, experiment) for _ in range(5)]
    session.add_all(trials)
    await session.flush()

    session.add_all(
        [_qa_row(cost_usd=0.01, trial_id=t.id) for t in trials[:3]]
    )
    await session.flush()

    costs = await get_trial_qa_costs(
        session, trial_ids=[t.id for t in trials], org_id=_ORG
    )

    assert len(costs) == 3
    assert all(costs[t.id] == pytest.approx(0.01) for t in trials[:3])
    assert all(t.id not in costs for t in trials[3:])


# =============================================================================
# Task 5: qa_cost_usd surfaced on the trial-detail and trials-table responses
# =============================================================================


@pytest.mark.asyncio
async def test_trial_detail_by_id_populates_qa_cost_usd(session):
    """``GET /trials/{trial_id}`` -- the trial detail panel's fetch."""
    task, experiment = await _fixture(session, "detail-byid")
    with_qa, without_qa = _trial(task, experiment), _trial(task, experiment)
    session.add_all([with_qa, without_qa])
    await session.flush()
    session.add(_qa_row(cost_usd=0.03, trial_id=with_qa.id))
    await session.flush()

    priced = await get_trial_response_for_org_core(
        session, trial_id=with_qa.id, org_id=_ORG
    )
    unpriced = await get_trial_response_for_org_core(
        session, trial_id=without_qa.id, org_id=_ORG
    )

    assert priced.qa_cost_usd == pytest.approx(0.03)
    # Absent, not zero -- the UI renders nothing rather than "+$0.00 QA".
    assert unpriced.qa_cost_usd is None


@pytest.mark.asyncio
async def test_trial_detail_by_index_populates_qa_cost_usd(session):
    """``GET /tasks/{task_id}/trials/{index}`` -- the other trial-detail path.

    ``get_trial_by_index_core`` looks the trial up by the conventional
    ``{task_id}-{index}`` id, so the fixture trial is given that id directly.
    """
    task, experiment = await _fixture(session, "detail-byidx")
    trial = _trial(task, experiment)
    trial.id = f"{task.id}-0"
    session.add(trial)
    await session.flush()
    session.add(_qa_row(cost_usd=0.02, trial_id=trial.id))
    await session.flush()

    response = await get_trial_by_index_core(
        session, task_id=task.id, index=0, org_id=_ORG
    )

    assert response.qa_cost_usd == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_slim_tasks_populate_per_trial_qa_cost(session):
    """The experiment grid's slim per-trial payload carries ``qa_cost_usd``."""
    task, experiment = await _fixture(session, "slim-qa")
    with_qa, without_qa = _trial(task, experiment), _trial(task, experiment)
    session.add_all([with_qa, without_qa])
    # A normal experiment owns its task via the association table directly.
    await session.execute(
        task_experiments.insert().values(task_id=task.id, experiment_id=experiment.id)
    )
    await session.flush()
    session.add(_qa_row(cost_usd=0.04, trial_id=with_qa.id))
    await session.flush()

    responses = await list_experiment_slim_tasks(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    task_resp = next(r for r in responses if r.id == task.id)
    by_id = {t.id: t for t in (task_resp.trials or [])}
    assert by_id[with_qa.id].qa_cost_usd == pytest.approx(0.04)
    assert by_id[without_qa.id].qa_cost_usd is None


@pytest.mark.asyncio
async def test_slim_tasks_resolve_qa_costs_in_one_query_for_the_page(
    session, monkeypatch
):
    """The grid pages many tasks/trials at once; QA must not fan out per trial."""
    task, experiment = await _fixture(session, "slim-batch")
    trials = [_trial(task, experiment) for _ in range(4)]
    session.add_all(trials)
    await session.execute(
        task_experiments.insert().values(task_id=task.id, experiment_id=experiment.id)
    )
    await session.flush()
    session.add_all([_qa_row(cost_usd=0.01, trial_id=t.id) for t in trials])
    await session.flush()

    import oddish.core.endpoints.tasks_query as tasks_query_mod

    call_count = 0
    real_get_trial_qa_costs = tasks_query_mod.get_trial_qa_costs

    async def _counting_get_trial_qa_costs(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return await real_get_trial_qa_costs(*args, **kwargs)

    monkeypatch.setattr(
        tasks_query_mod, "get_trial_qa_costs", _counting_get_trial_qa_costs
    )

    await list_experiment_slim_tasks(session, experiment_id=experiment.id, org_id=_ORG)

    assert call_count == 1


@pytest.mark.asyncio
async def test_sharing_trial_list_leaves_qa_cost_unpopulated(session):
    """Sharing/public views deliberately omit QA spend -- internal cost data
    has no place in a public trial view."""
    task, experiment = await _fixture(session, "sharing-noqa")
    trial = _trial(task, experiment)
    session.add(trial)
    await session.flush()
    session.add(_qa_row(cost_usd=0.05, trial_id=trial.id))
    await session.flush()

    trials = await list_experiment_trials_for_org(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    (response,) = [t for t in trials if t.id == trial.id]
    assert response.qa_cost_usd is None
