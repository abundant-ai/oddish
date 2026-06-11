from oddish.db.models import TrialModel


def test_trial_model_has_probe_scope_column():
    col = TrialModel.__table__.columns["probe_scope"]
    assert col.nullable is True
    assert col.index is True
