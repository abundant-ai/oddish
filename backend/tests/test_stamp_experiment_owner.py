from __future__ import annotations

from api.routers.tasks import _stamp_experiment_owner
from oddish.core.dashboard import EXPERIMENTS_UNATTRIBUTED_OWNER
from oddish.db import ExperimentModel


def _experiment(owner: str | None) -> ExperimentModel:
    return ExperimentModel(id="exp_1", name="exp", org_id="org_1", owner_user_id=owner)


def test_stamps_when_owner_missing() -> None:
    experiment = _experiment(None)
    _stamp_experiment_owner(experiment, "user_1")
    assert experiment.owner_user_id == "user_1"


def test_overwrites_unattributed_sentinel() -> None:
    experiment = _experiment(EXPERIMENTS_UNATTRIBUTED_OWNER)
    _stamp_experiment_owner(experiment, "user_1")
    assert experiment.owner_user_id == "user_1"


def test_keeps_existing_real_owner() -> None:
    experiment = _experiment("user_2")
    _stamp_experiment_owner(experiment, "user_1")
    assert experiment.owner_user_id == "user_2"


def test_ignores_missing_inputs() -> None:
    experiment = _experiment(None)
    _stamp_experiment_owner(experiment, None)
    assert experiment.owner_user_id is None
    _stamp_experiment_owner(None, "user_1")  # must not raise
