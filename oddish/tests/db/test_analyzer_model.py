from oddish.db.models import AnalyzerModel, analyzer_experiments, JobStatus, generate_id


def test_analyzer_model_table_and_columns():
    assert AnalyzerModel.__tablename__ == "analyzers"
    cols = set(AnalyzerModel.__table__.columns.keys())
    assert {
        "id", "name", "org_id", "owner_user_id", "owner",
        "status", "error",
        "bad_failure_content", "good_failure_content",
        "universal_capabilities_content", "headroom_analysis",
        "num_trials", "num_bad_failures", "num_good_failures",
        "breakdown", "started_at", "finished_at",
        "created_at", "updated_at", "deleted_at",
    } <= cols


def test_analyzer_defaults_and_id_autogen():
    # Column-level ``default=`` is a flush-time default (SQLAlchemy applies
    # it during INSERT, not object construction -- ExperimentModel exhibits
    # the same behavior), so we assert on the column metadata rather than a
    # freshly-constructed instance's attributes, mirroring the pattern in
    # test_experiment_trials_schema.py.
    id_col = AnalyzerModel.__table__.columns["id"]
    assert id_col.default is not None
    assert id_col.default.is_callable
    assert len(generate_id()) == 8

    status_col = AnalyzerModel.__table__.columns["status"]
    assert status_col.default is not None
    assert status_col.default.arg == JobStatus.PENDING


def test_analyzer_experiments_join_columns():
    cols = set(analyzer_experiments.c.keys())
    assert cols == {"analyzer_id", "experiment_id", "created_at", "deleted_at"}
