from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.dashboard import get_model_usage_core
from oddish.db.models import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    generate_id,
)


def _trial(
    task: TaskModel,
    experiment: ExperimentModel,
    *,
    model: str,
    provider: str = "openai",
    cost_usd: float | None = None,
    output_tokens: int | None = None,
) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        experiment_id=experiment.id,
        agent="codex",
        provider=provider,
        queue_key=f"{provider}/{model}",
        model=model,
        status=TrialStatus.SUCCESS,
        cost_usd=cost_usd,
        output_tokens=output_tokens,
    )


async def _seed(session, trials_spec):
    task = TaskModel(
        name=f"usage-{generate_id()}",
        user="tester",
        task_path="s3://tasks/usage",
    )
    session.add(task)
    await session.flush()
    experiment = ExperimentModel(name=f"usage-exp-{generate_id()}")
    session.add(experiment)
    await session.flush()
    for spec in trials_spec:
        session.add(_trial(task, experiment, **spec))
    await session.flush()


@pytest.mark.asyncio
async def test_model_usage_falls_back_to_token_estimate(session):
    """Unpriced trials (NULL cost_usd) contribute a token estimate, not $0."""
    # Unique model id so this test's rows can't collide with other data.
    model = f"gpt-5.3-{generate_id()}"
    await _seed(
        session,
        [
            # gpt-5.3 is priceable ($14/1M output); NULL cost + 1M output tokens
            # settles to a $14 estimate instead of $0.
            {"model": model, "output_tokens": 1_000_000},
            # Sibling on the same model with a recorded native cost adds $5.
            {"model": model, "cost_usd": 5.0},
        ],
    )

    usage = await get_model_usage_core(session)
    by_model = {row["model"]: row for row in usage}
    assert model in by_model
    row = by_model[model]
    # $5 native + $14 token estimate.
    assert row["cost_usd"] == pytest.approx(19.0, abs=1e-6)
    # Estimated portion is only the token-derived $14.
    assert row["cost_estimated_usd"] == pytest.approx(14.0, abs=1e-6)


@pytest.mark.asyncio
async def test_model_usage_unpriceable_model_reports_zero_estimate(session):
    """An unpriceable model yields no estimate (that gap is the alert's job)."""
    model = f"made-up/no-such-model-{generate_id()}"
    await _seed(
        session,
        [{"model": model, "provider": "made-up", "output_tokens": 1_000_000}],
    )

    usage = await get_model_usage_core(session)
    by_model = {row["model"]: row for row in usage}
    row = by_model[model]
    # No rate resolves -> $0 (the unpriced floor), and none of it is estimated.
    assert row["cost_usd"] == pytest.approx(0.0, abs=1e-6)
    assert row["cost_estimated_usd"] == pytest.approx(0.0, abs=1e-6)
