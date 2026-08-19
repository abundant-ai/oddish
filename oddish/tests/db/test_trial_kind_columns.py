from oddish.db.models import AGENT_TRIAL_KIND, ExperimentModel, TrialModel


def test_trial_kind_column_defaults_to_agent():
    col = TrialModel.__table__.columns["kind"]
    assert not col.nullable
    assert col.server_default.arg == AGENT_TRIAL_KIND


def test_experiment_shadow_of_column_is_nullable():
    assert ExperimentModel.__table__.columns["shadow_of"].nullable


def test_kind_and_shadow_indexes_declared_on_models():
    # 000_initial_schema runs Base.metadata.create_all(), so the model must
    # declare the same index names the migrations create -- otherwise fresh
    # DBs and migrated DBs end up with different (or duplicate) indexes.
    assert "ix_trials_kind_non_agent" in {
        ix.name for ix in TrialModel.__table__.indexes
    }
    assert "uq_experiments_shadow_of_live" in {
        ix.name for ix in ExperimentModel.__table__.indexes
    }
