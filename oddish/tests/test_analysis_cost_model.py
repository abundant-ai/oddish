from oddish.db.models import AnalysisCostModel


def test_table_name_and_columns() -> None:
    assert AnalysisCostModel.__tablename__ == "analysis_costs"
    cols = set(AnalysisCostModel.__table__.columns.keys())
    assert {
        "id",
        "job_kind",
        "trial_id",
        "experiment_id",
        "org_id",
        "billed_user_id",
        "model",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_usd",
        "cost_source",
        "created_at",
    } <= cols


def test_trial_id_has_no_foreign_key() -> None:
    # A hard FK to trials risks the migration lock that deadlocked prod.
    assert AnalysisCostModel.__table__.c.trial_id.foreign_keys == set()
