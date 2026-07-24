from __future__ import annotations

from api.routers.task_submission import stamp_experiment_api_key
from oddish.db import ExperimentModel


def _experiment(api_key_id: str | None) -> ExperimentModel:
    return ExperimentModel(id="exp_1", name="exp", org_id="org_1", api_key_id=api_key_id)


def test_stamps_when_key_missing() -> None:
    experiment = _experiment(None)
    stamp_experiment_api_key(experiment, "key_1")
    assert experiment.api_key_id == "key_1"


def test_keeps_existing_key() -> None:
    experiment = _experiment("key_2")
    stamp_experiment_api_key(experiment, "key_1")
    assert experiment.api_key_id == "key_2"


def test_ignores_missing_inputs() -> None:
    # A JWT/dashboard submission has no key; the column stays NULL rather than
    # being backfilled from some later run.
    experiment = _experiment(None)
    stamp_experiment_api_key(experiment, None)
    assert experiment.api_key_id is None
    stamp_experiment_api_key(None, "key_1")  # must not raise
