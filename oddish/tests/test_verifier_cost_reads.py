"""Read schemas keep CUA spend off solver and billed totals."""

from __future__ import annotations

from oddish.schemas import ExperimentCostTotals, TaskCostTotals, TrialResponse


def test_schema_defaults_keep_solver_cost_untouched() -> None:
    trial = TrialResponse.model_construct(cost_usd=3.5)
    assert trial.verifier_cost_usd is None
    assert trial.cost_usd == 3.5

    task = TaskCostTotals(cost_usd=1.0)
    assert task.verifier_cost_usd == 0.0
    assert task.cost_usd == 1.0

    experiment = ExperimentCostTotals(cost_usd=2.0, billed_cost_usd=2.0)
    assert experiment.verifier_cost_usd == 0.0
    assert experiment.owned_verifier_cost_usd == 0.0
    assert experiment.verifier_has_estimated is False
    assert experiment.cost_usd == 2.0
    assert experiment.billed_cost_usd == 2.0
