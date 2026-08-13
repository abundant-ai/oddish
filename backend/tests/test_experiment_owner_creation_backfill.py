from __future__ import annotations

import importlib.util
from pathlib import Path


def test_backfill_uses_the_exact_first_trial_including_history() -> None:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic/versions/expownercreate001_backfill_creation_owner.py"
    )
    spec = importlib.util.spec_from_file_location(
        "experiment_owner_backfill", migration_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    statements: list[str] = []
    original_execute = module.op.execute
    module.op.execute = statements.append
    try:
        module.upgrade()
    finally:
        module.op.execute = original_execute

    sql = statements[0]
    assert "DISTINCT ON (t.experiment_id)" in sql
    assert "ORDER BY t.experiment_id, t.created_at ASC, t.id ASC" in sql
    assert "earliest_trial.billed_user_id IS NOT NULL" in sql
    assert "owner_user_id IS NULL" in sql
    assert "t.deleted_at" not in sql
    assert "superseded_by_trial_id" not in sql
