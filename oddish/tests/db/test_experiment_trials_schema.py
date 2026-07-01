from oddish.db import experiment_trials
from oddish.db.models import ExperimentModel


def test_experiment_trials_columns_and_pk():
    cols = {c.name: c for c in experiment_trials.columns}
    assert set(cols) == {"experiment_id", "trial_id", "created_at", "deleted_at"}
    assert cols["experiment_id"].type.length == 64
    assert cols["trial_id"].type.length == 160
    pk = {c.name for c in experiment_trials.primary_key.columns}
    assert pk == {"experiment_id", "trial_id"}


def test_experiment_is_collection_column_defaults_false():
    assert hasattr(ExperimentModel, "is_collection")
    col = ExperimentModel.__table__.columns["is_collection"]
    assert col.nullable is False
