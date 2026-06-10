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


def test_append_path_reclaims_sentinel_only() -> None:
    sentineled = _experiment(EXPERIMENTS_UNATTRIBUTED_OWNER)
    _stamp_experiment_owner(sentineled, "user_1", claim_unowned=False)
    assert sentineled.owner_user_id == "user_1"


def test_append_path_leaves_null_for_sweep_attribution() -> None:
    # An appender is not necessarily the primary author; NULL owners belong
    # to the sweep's precedence-based claim, not to whoever reruns first.
    unowned = _experiment(None)
    _stamp_experiment_owner(unowned, "user_1", claim_unowned=False)
    assert unowned.owner_user_id is None
