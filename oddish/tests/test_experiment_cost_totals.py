"""``get_experiment_cost_totals`` — the whole-experiment cost rollup.

Two things are being pinned here.

*Pricing is exact.* The aggregate groups by ``(agent, model, billed)`` and, for
trials whose ``cost_usd`` is NULL, prices pooled token totals rather than
dollars. It must equal what per-trial pricing would produce, so those tests
assert against ``_resolve_trial_cost`` — the same function the trial API
serializes through — instead of hand-computed numbers.

*Scope is spend, not the grid.* Cost counts every member trial: all task
versions, superseded retries, probes, and soft-deleted trials. The grid hides
those (see ``get_task_status_trials``) so that scores compare like with like,
but the money was spent and billed regardless — ``core.quotas`` and the admin
cost breakdown apply none of those filters either. Several tests below exist
specifically to keep someone from "fixing" the tile back into agreement with
the visible rows.

*Two scopes, not one.* ``cost_*`` counts every trial the page renders — homed
or gathered (``trial_in_experiment``, the grid's membership) — so collections
show what their work cost. ``owned_*`` counts only homed trials (the "New
spend" tile) and is the number that stays additive across experiments;
``billed_*`` is its billed subset. ``task_experiments`` links never widen
cost.

Uses the rollback-per-test ``session`` fixture against the local Postgres, with
a run-scoped org id so each test sees only its own trials.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import insert

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.experiment_cost import (  # noqa: E402
    get_experiment_cost_totals,
)
from oddish.core.helpers import (  # noqa: E402
    _resolve_trial_cost,
    get_task_status_trials,
)
from oddish.config import settings  # noqa: E402
from oddish.db.models import (  # noqa: E402
    ExperimentModel,
    experiment_trials,
    task_experiments,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    generate_id,
    utcnow,
)

_ORG = f"expcost-org-{uuid.uuid4().hex[:8]}"


def _trial(
    task: TaskModel,
    experiment: ExperimentModel,
    *,
    agent: str = "codex",
    model: str = "gpt-5.5",
    cost_usd: float | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_tokens: int | None = None,
    cache_write_tokens: int | None = None,
    billed_user_id: str | None = None,
    is_probe: bool = False,
    superseded_by_trial_id: str | None = None,
) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        experiment_id=experiment.id,
        org_id=_ORG,
        agent=agent,
        provider="openai",
        queue_key=f"openai/{model}",
        model=model,
        status=TrialStatus.SUCCESS,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_tokens=cache_tokens,
        cache_write_tokens=cache_write_tokens,
        billed_user_id=billed_user_id,
        is_probe=is_probe,
        superseded_by_trial_id=superseded_by_trial_id,
        created_at=utcnow(),
    )


async def _fixture(session, slug: str) -> tuple[TaskModel, ExperimentModel]:
    task = TaskModel(
        name=f"expcost-task-{slug}-{_ORG}",
        org_id=_ORG,
        user="tester",
        task_path=f"s3://tasks/expcost-{slug}",
    )
    session.add(task)
    await session.flush()

    experiment = ExperimentModel(
        name=f"expcost-exp-{slug}-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add(experiment)
    await session.flush()
    return task, experiment


def _client_side_sum(trials: list[TrialModel]) -> float:
    """What the frontend's ``accumulateTrial`` fold would produce over ``trials``."""
    total = 0.0
    for trial in trials:
        cost, _estimated = _resolve_trial_cost(
            trial, settings.normalize_trial_model(trial.agent, trial.model)
        )
        if cost is not None:
            total += cost
    return total


@pytest.mark.asyncio
async def test_totals_match_per_trial_sum_across_native_and_estimated(session):
    """The grouped aggregate reproduces the per-trial sum exactly.

    Mixes native-cost trials with NULL-cost trials that must be priced from
    tokens, across two different models, so a bare ``SUM(cost_usd)`` would
    visibly understate the result.
    """
    task, experiment = await _fixture(session, "mixed")

    trials = [
        # Native cost reported by the runtime.
        _trial(task, experiment, cost_usd=1.25),
        _trial(task, experiment, cost_usd=0.75),
        # NULL cost -> priced from tokens at read time.
        _trial(
            task,
            experiment,
            input_tokens=1_000_000,
            output_tokens=100_000,
            cache_tokens=250_000,
            cache_write_tokens=50_000,
        ),
        # A different model, so it lands in its own pricing group.
        _trial(
            task,
            experiment,
            agent="claude-code",
            model="zai/glm-x-preview[1m]",
            input_tokens=2_000_000,
            output_tokens=300_000,
        ),
    ]
    session.add_all(trials)
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    expected = _client_side_sum(trials)
    assert totals.cost_usd == pytest.approx(expected)
    assert totals.cost_trial_count == 4
    assert totals.total_trials == 4
    assert totals.cost_has_native is True
    assert totals.cost_has_estimated is True

    # Everything is homed here, so owned spend IS the cost.
    assert totals.owned_cost_usd == pytest.approx(expected)
    assert totals.owned_trial_count == 4
    assert totals.owned_has_native is True
    assert totals.owned_has_estimated is True

    # The bug this endpoint exists to fix: dollars-only summing drops the
    # estimated trials, so it must come out strictly lower.
    native_only = sum(t.cost_usd for t in trials if t.cost_usd is not None)
    assert native_only < totals.cost_usd


@pytest.mark.asyncio
async def test_pooled_token_estimate_equals_per_trial_estimate(session):
    """Pooling tokens per model before pricing is exact, not an approximation.

    ``estimate_cost_usd`` clamps ``max(0, input - cached - cache_write)`` per
    row, so the clamp is reproduced in SQL. This trial set includes a row where
    cached + cache_write EXCEEDS input, which is precisely where a naive
    sum-then-clamp would diverge from clamp-then-sum.
    """
    task, experiment = await _fixture(session, "clamp")

    trials = [
        _trial(
            task,
            experiment,
            input_tokens=100_000,
            output_tokens=10_000,
            cache_tokens=90_000,
            cache_write_tokens=40_000,  # cached + write > input -> clamps to 0
        ),
        _trial(
            task,
            experiment,
            input_tokens=1_000_000,
            output_tokens=20_000,
            cache_tokens=100_000,
            cache_write_tokens=10_000,  # no clamping
        ),
    ]
    session.add_all(trials)
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.cost_usd == pytest.approx(_client_side_sum(trials))
    assert totals.cost_trial_count == 2
    assert totals.cost_has_estimated is True
    assert totals.cost_has_native is False


@pytest.mark.asyncio
async def test_billed_split_tracks_billed_user_id_within_owned(session):
    """``billed_*`` covers only OWNED trials carrying a ``billed_user_id``.

    A gathered trial that was billed under its home experiment must not leak
    into this experiment's billed subset — billed is a slice of owned spend,
    the additive number, or billed totals double-count across pages too.
    """
    task, experiment = await _fixture(session, "billed")

    trials = [
        _trial(task, experiment, cost_usd=2.0, billed_user_id="user_a"),
        _trial(
            task,
            experiment,
            input_tokens=1_000_000,
            output_tokens=100_000,
            billed_user_id="user_b",
        ),
        _trial(task, experiment, cost_usd=5.0),  # unbilled
    ]
    session.add_all(trials)
    await session.flush()

    other = ExperimentModel(
        name=f"expcost-billed-other-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add(other)
    await session.flush()
    borrowed_billed = _trial(task, other, cost_usd=11.0, billed_user_id="user_c")
    session.add(borrowed_billed)
    await session.flush()
    await session.execute(
        insert(experiment_trials).values(
            [{"experiment_id": experiment.id, "trial_id": borrowed_billed.id}]
        )
    )
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    billed_expected = _client_side_sum(trials[:2])
    assert totals.billed_cost_usd == pytest.approx(billed_expected)
    assert totals.billed_trial_count == 2
    assert totals.billed_has_native is True
    assert totals.billed_has_estimated is True
    # Owned spend includes the unbilled trial; cost additionally includes the
    # gathered one.
    assert totals.owned_cost_usd == pytest.approx(_client_side_sum(trials))
    assert totals.owned_trial_count == 3
    assert totals.cost_usd == pytest.approx(_client_side_sum(trials) + 11.0)
    assert totals.cost_trial_count == 4


@pytest.mark.asyncio
async def test_probe_and_superseded_trials_still_count_as_spend(session):
    """Cost is spend, so trials the GRID hides must still be summed.

    A probe and a superseded retry attempt both really ran and were really
    billed (``core.quotas`` applies no probe/superseded filter). The grid omits
    them for score-comparability reasons; that must not erase their cost.
    """
    task, experiment = await _fixture(session, "scope")

    keeper = _trial(task, experiment, cost_usd=3.0)
    session.add(keeper)
    await session.flush()

    session.add_all(
        [
            _trial(task, experiment, cost_usd=5.0, is_probe=True),
            _trial(task, experiment, cost_usd=7.0, superseded_by_trial_id=keeper.id),
        ]
    )
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.cost_usd == pytest.approx(15.0)
    assert totals.cost_trial_count == 3
    assert totals.total_trials == 3


@pytest.mark.asyncio
async def test_gathered_trials_count_as_cost_but_not_owned_spend(session):
    """A collection's cost is what the gathered work cost; owned spend is not.

    ``cost_*`` answers "what did the work on this page cost", so trials
    gathered through ``experiment_trials`` count — a collection assembled
    entirely from other experiments' work must not show a blank tile.
    ``owned_*`` stays home-only (the additive number), and a gathered row
    pointing at an owned trial must not double-count it in either scope.
    """
    task, home = await _fixture(session, "gathered")

    collection = ExperimentModel(
        name=f"expcost-collection-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add(collection)
    await session.flush()

    borrowed = _trial(task, home, cost_usd=4.0)  # home is the OTHER experiment
    native_and_gathered = _trial(task, collection, cost_usd=6.0)  # home == collection
    session.add_all([borrowed, native_and_gathered])
    await session.flush()

    await session.execute(
        insert(experiment_trials).values(
            [
                {"experiment_id": collection.id, "trial_id": borrowed.id},
                {"experiment_id": collection.id, "trial_id": native_and_gathered.id},
            ]
        )
    )
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=collection.id, org_id=_ORG
    )
    assert totals.cost_usd == pytest.approx(10.0)
    assert totals.cost_trial_count == 2
    assert totals.total_trials == 2
    assert totals.owned_cost_usd == pytest.approx(6.0)
    assert totals.owned_trial_count == 1

    # The home experiment still reports its own trial: the same dollars appear
    # under both pages' cost_* (deliberate), but owned_* never overlaps.
    home_totals = await get_experiment_cost_totals(
        session, experiment_id=home.id, org_id=_ORG
    )
    assert home_totals.cost_usd == pytest.approx(4.0)
    assert home_totals.owned_cost_usd == pytest.approx(4.0)
    assert home_totals.total_trials == 1


@pytest.mark.asyncio
async def test_task_link_rows_do_not_widen_cost(session):
    """``task_experiments`` links contribute nothing on their own.

    Collections link each gathered trial's TASK so the task row renders, but
    the grid still scopes that task's trials to home-or-gathered
    (``trial_in_experiment``). Counting a linked task's whole history would
    price trials that were never selected — and let a frozen collection's
    cost drift upward whenever anyone reruns the same task elsewhere. Only
    the explicitly gathered trial counts.
    """
    task, home = await _fixture(session, "linked")

    other = ExperimentModel(
        name=f"expcost-linked-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add(other)
    await session.flush()

    selected = _trial(task, home, cost_usd=5.0)
    unselected = _trial(task, home, cost_usd=100.0)  # same task, never gathered
    session.add_all([selected, unselected])
    await session.flush()
    await session.execute(
        insert(task_experiments).values(
            [{"task_id": task.id, "experiment_id": other.id}]
        )
    )
    await session.execute(
        insert(experiment_trials).values(
            [{"experiment_id": other.id, "trial_id": selected.id}]
        )
    )
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=other.id, org_id=_ORG
    )
    assert totals.cost_usd == pytest.approx(5.0)
    assert totals.cost_trial_count == 1
    assert totals.total_trials == 1
    assert totals.owned_cost_usd == 0.0
    assert totals.owned_trial_count == 0


@pytest.mark.asyncio
async def test_soft_deleted_membership_rows_stop_counting(session):
    """Removing a trial/task from a collection removes its cost here.

    Membership rows are the one place soft-deletes apply: a deleted
    ``experiment_trials``/``task_experiments`` row means "no longer shown
    here", unlike a soft-deleted TRIAL, whose spend keeps counting on its home
    experiment.
    """
    task, home = await _fixture(session, "unlinked")

    collection = ExperimentModel(
        name=f"expcost-unlinked-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add(collection)
    await session.flush()

    borrowed = _trial(task, home, cost_usd=4.0)
    session.add(borrowed)
    await session.flush()
    await session.execute(
        insert(experiment_trials).values(
            [
                {
                    "experiment_id": collection.id,
                    "trial_id": borrowed.id,
                    "deleted_at": utcnow(),
                }
            ]
        )
    )
    await session.execute(
        insert(task_experiments).values(
            [
                {
                    "task_id": task.id,
                    "experiment_id": collection.id,
                    "deleted_at": utcnow(),
                }
            ]
        )
    )
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=collection.id, org_id=_ORG
    )
    assert totals.cost_usd == 0.0
    assert totals.total_trials == 0


@pytest.mark.asyncio
async def test_negative_token_counts_cannot_cancel_across_trials(session):
    """Token clamps are per row, matching ``estimate_cost_usd``.

    Pathological data only, but if the SQL summed raw and clamped once at the
    pooled total, one trial's negative count would cancel a sibling's positive
    one — something per-trial pricing can never do.
    """
    task, experiment = await _fixture(session, "negative")

    trials = [
        _trial(task, experiment, input_tokens=100_000, cache_tokens=-50_000),
        _trial(task, experiment, input_tokens=100_000, cache_tokens=50_000),
    ]
    session.add_all(trials)
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.cost_usd == pytest.approx(_client_side_sum(trials))


@pytest.mark.asyncio
async def test_soft_deleted_trials_still_count_as_spend(session):
    """Deleting a trial removes it from the view; it does not refund it.

    The query opts out of the global soft-delete filter with
    ``include_deleted=True``, matching the two other places that answer "what
    was spent": the quota sum (``core.quotas``) and the admin cost breakdown.
    Without this the page would report a smaller number than the admin table and
    the invoice for the same experiment.
    """
    task, experiment = await _fixture(session, "deleted")

    kept = _trial(task, experiment, cost_usd=3.0)
    removed = _trial(task, experiment, cost_usd=9.0)
    removed.deleted_at = utcnow()
    session.add_all([kept, removed])
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.cost_usd == pytest.approx(12.0)
    assert totals.total_trials == 2


@pytest.mark.asyncio
async def test_unlinked_trials_from_other_experiments_are_never_counted(session):
    """Membership is the one filter that DOES apply.

    Sharing a TASK object between experiments (each running its own trials on
    it) creates no membership rows, so the other experiment's trials don't
    show in this grid and must not show in this cost either.
    """
    task, experiment = await _fixture(session, "isolation")

    other = ExperimentModel(
        name=f"expcost-other-{_ORG}", org_id=_ORG, last_activity_at=utcnow()
    )
    session.add(other)
    await session.flush()

    session.add_all(
        [
            _trial(task, experiment, cost_usd=4.0),
            _trial(task, other, cost_usd=1000.0),  # same task, different experiment
        ]
    )
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.cost_usd == pytest.approx(4.0)
    assert totals.owned_cost_usd == pytest.approx(4.0)
    assert totals.total_trials == 1


@pytest.mark.asyncio
async def test_untokened_and_unpriced_trials_contribute_nothing(session):
    """A trial with no native cost and nothing to price on resolves to no cost.

    Mirrors ``_resolve_trial_cost``: NULL cost with both token columns NULL, or
    with a model the pricing table cannot resolve, yields ``None`` — it must not
    inflate ``cost_trial_count`` nor set the estimated flag.
    """
    task, experiment = await _fixture(session, "unpriced")

    trials = [
        _trial(task, experiment, cost_usd=4.0),
        # Never reported usage at all.
        _trial(task, experiment),
        # Tokens, but a model with no pricing entry.
        _trial(
            task,
            experiment,
            model=f"totally-unknown-model-{uuid.uuid4().hex[:6]}",
            input_tokens=500_000,
            output_tokens=50_000,
        ),
    ]
    session.add_all(trials)
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.cost_usd == pytest.approx(_client_side_sum(trials)) == 4.0
    assert totals.cost_trial_count == 1
    assert totals.total_trials == 3
    assert totals.cost_has_estimated is False
    assert totals.cost_has_native is True


@pytest.mark.asyncio
async def test_trials_on_earlier_task_versions_still_count_as_spend(session):
    """Re-uploading a task mid-experiment must not erase the older run's cost.

    The grid pivots each task to its effective version
    (``get_task_status_trials``), so v1's trials vanish from the table once v2
    exists. They still ran and were still billed, so the cost tile — which
    reports spend — must include them. This is the case where the tile and the
    visible rows legitimately disagree.
    """
    task, experiment = await _fixture(session, "versions")

    v1 = TaskVersionModel(
        id=f"{task.id}-v1", task_id=task.id, version=1, task_path=task.task_path
    )
    v2 = TaskVersionModel(
        id=f"{task.id}-v2", task_id=task.id, version=2, task_path=task.task_path
    )
    session.add_all([v1, v2])
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()

    old = _trial(task, experiment, cost_usd=50.0)
    old.task_version_id = v1.id
    new_native = _trial(task, experiment, cost_usd=2.0)
    new_native.task_version_id = v2.id
    session.add_all([old, new_native])
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    # The grid shows only the v2 trial...
    await session.refresh(task, attribute_names=["trials"])
    displayed = get_task_status_trials(task, version_id=v2.id)
    assert {t.id for t in displayed} == {new_native.id}

    # ...but the tile reports what the experiment actually cost: v1 + v2.
    assert totals.cost_usd == pytest.approx(52.0)
    assert totals.cost_trial_count == 2
    assert totals.total_trials == 2
    assert totals.cost_usd > _client_side_sum(displayed)


@pytest.mark.asyncio
async def test_empty_experiment_yields_zeroed_totals(session):
    _task, experiment = await _fixture(session, "empty")

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.cost_usd == 0.0
    assert totals.total_trials == 0
    assert totals.cost_has_native is False
    assert totals.cost_has_estimated is False
    assert totals.owned_cost_usd == 0.0
    assert totals.owned_trial_count == 0
