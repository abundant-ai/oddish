from oddish.db.models import ReportModel, report_experiments, JobStatus, generate_id


def test_report_model_table_and_columns():
    assert ReportModel.__tablename__ == "reports"
    cols = set(ReportModel.__table__.columns.keys())
    assert {
        "id", "name", "org_id", "owner_user_id", "owner",
        "status", "error",
        "bad_failure_content", "good_failure_content",
        "universal_capabilities_content", "headroom_analysis",
        "num_trials", "num_bad_failures", "num_good_failures",
        "breakdown", "started_at", "finished_at",
        "created_at", "updated_at", "deleted_at",
    } <= cols


def test_report_defaults_and_id_autogen():
    # Column-level ``default=`` is a flush-time default (SQLAlchemy applies
    # it during INSERT, not object construction -- ExperimentModel exhibits
    # the same behavior), so we assert on the column metadata rather than a
    # freshly-constructed instance's attributes, mirroring the pattern in
    # test_experiment_trials_schema.py.
    id_col = ReportModel.__table__.columns["id"]
    assert id_col.default is not None
    assert id_col.default.is_callable
    assert len(generate_id()) == 8

    status_col = ReportModel.__table__.columns["status"]
    assert status_col.default is not None
    assert status_col.default.arg == JobStatus.PENDING


def test_report_experiments_join_columns():
    cols = set(report_experiments.c.keys())
    assert cols == {"report_id", "experiment_id", "created_at", "deleted_at"}
