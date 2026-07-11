"""``get_experiment_cost_totals`` — the whole-experiment cost rollup.

The experiment page pages its trials, so the cost tiles cannot be a client-side
sum. The server-side aggregate groups by ``(agent, model, billed)`` and prices
pooled token totals for trials whose ``cost_usd`` is NULL. The load-bearing
claim is that this equals the per-trial sum the UI *would* have produced had
every page been loaded — so the tests below assert exactly that, against
``_resolve_trial_cost`` (the same function the trial API serializes through).

Uses the rollback-per-test ``session`` fixture against the local Postgres, with
a run-scoped org id so each test sees only its own trials.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.experiment_cost import (  # noqa: E402
    get_experiment_cost_totals,
)
from oddish.core.helpers import _resolve_trial_cost  # noqa: E402
from oddish.config import settings  # noqa: E402
from oddish.db.models import (  # noqa: E402
    ExperimentModel,
    TaskModel,
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
async def test_billed_split_tracks_billed_user_id(session):
    """``billed_*`` covers only trials carrying a ``billed_user_id``."""
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

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    billed_expected = _client_side_sum(trials[:2])
    assert totals.billed_cost_usd == pytest.approx(billed_expected)
    assert totals.billed_trial_count == 2
    assert totals.billed_has_native is True
    assert totals.billed_has_estimated is True
    assert totals.cost_usd == pytest.approx(_client_side_sum(trials))
    assert totals.cost_trial_count == 3


@pytest.mark.asyncio
async def test_probe_and_superseded_trials_are_excluded(session):
    """Scope matches the grid: no probes, no superseded reruns."""
    task, experiment = await _fixture(session, "scope")

    keeper = _trial(task, experiment, cost_usd=3.0)
    session.add(keeper)
    await session.flush()

    session.add_all(
        [
            _trial(task, experiment, cost_usd=100.0, is_probe=True),
            _trial(task, experiment, cost_usd=100.0, superseded_by_trial_id=keeper.id),
        ]
    )
    await session.flush()

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.cost_usd == pytest.approx(3.0)
    assert totals.cost_trial_count == 1
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
async def test_empty_experiment_yields_zeroed_totals(session):
    _task, experiment = await _fixture(session, "empty")

    totals = await get_experiment_cost_totals(
        session, experiment_id=experiment.id, org_id=_ORG
    )

    assert totals.cost_usd == 0.0
    assert totals.total_trials == 0
    assert totals.cost_has_native is False
    assert totals.cost_has_estimated is False
