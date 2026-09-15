from oddish.db.models import VerifierCostModel


def test_table_name_and_columns() -> None:
    assert VerifierCostModel.__tablename__ == "verifier_costs"
    cols = set(VerifierCostModel.__table__.columns.keys())
    assert {
        "id",
        "trial_id",
        "attempt",
        "component",
        "experiment_id",
        "org_id",
        "billed_user_id",
        "task_id",
        "task_version_id",
        "model",
        "route",
        "llm_key_hash",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "cost_usd",
        "cost_source",
        "unpriced_reason",
        "created_at",
    } <= cols


def test_trial_id_has_no_foreign_key() -> None:
    assert VerifierCostModel.__table__.c.trial_id.foreign_keys == set()
