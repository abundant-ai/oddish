from oddish.core.experiment_membership import (
    gathered_trial_ids_select,
    trial_in_experiment,
)


def test_gathered_select_compiles_and_filters_live():
    sql = str(gathered_trial_ids_select("exp1"))
    assert "experiment_trials" in sql
    assert "deleted_at IS NULL" in sql.replace('"', "")


def test_trial_in_experiment_unions_scalar_and_join():
    sql = str(trial_in_experiment("exp1"))
    assert "experiment_id" in sql
    assert "IN" in sql.upper()
