from oddish.db.models import TrialModel, WorkerJobModel


def test_trial_model_has_harbor_sha_column():
    col = TrialModel.__table__.c.harbor_sha
    assert col.nullable is True
    assert col.index is True


def test_worker_job_model_has_harbor_variant_id_column():
    col = WorkerJobModel.__table__.c.harbor_variant_id
    assert col.nullable is False
    assert col.server_default is not None
