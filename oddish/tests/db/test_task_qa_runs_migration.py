from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from oddish.db import TaskModel, TaskQaRunModel, get_soft_delete_models


def _migration_text() -> str:
    return (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "task_qa_runs_001_add_task_qa_runs.py"
    ).read_text()


def test_task_qa_run_schema_matches_the_orm() -> None:
    columns = TaskQaRunModel.__table__.columns
    assert {
        "task_id",
        "task_version_id",
        "worker_job_id",
        "disposition",
        "input_trial_ids",
        "input_analysis_fingerprints",
        "verdict",
        "error",
        "pre_trial_block_id",
        "verdict_block_id",
        "started_at",
        "finished_at",
    } <= set(columns.keys())
    assert columns.task_version_id.nullable is False
    assert columns.worker_job_id.nullable is False
    assert "uq_task_qa_runs_worker_job_id" in {
        constraint.name for constraint in TaskQaRunModel.__table__.constraints
    }
    assert "idx_task_qa_runs_version_created" in {
        index.name for index in TaskQaRunModel.__table__.indexes
    }
    assert TaskQaRunModel not in get_soft_delete_models()

    task_columns = TaskModel.__table__.columns
    assert "published_qa_run_id" in task_columns
    assert "verdict_version_id" in task_columns


def test_migration_backfills_only_active_versioned_full_qa_jobs() -> None:
    migration = _migration_text()
    assert "task_qa_runs_001" in migration
    assert 'down_revision: str | Sequence[str] | None = "agentcap01"' in migration
    assert "active full-QA worker job lacks task_version_id" in migration
    assert "COALESCE(payload->>'mode', '') <> 'pre_trial'" in migration
    assert "status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')" in migration
    assert "INSERT INTO task_qa_runs" in migration
    assert "jsonb_set(w.payload, '{qa_run_id}'" in migration
    # Historical task verdicts are deliberately not assigned to any guessed run.
    assert "published_qa_run_id =" not in migration


def test_task_qa_run_migration_is_the_single_head_and_downgrades_in_fk_order() -> None:
    oddish_root = Path(__file__).resolve().parents[2]
    config = Config(str(oddish_root / "alembic.ini"))
    config.set_main_option("script_location", str(oddish_root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == ["task_qa_runs_001"]

    migration = _migration_text()
    downgrade = migration.split("def downgrade() -> None:", 1)[1]
    pointer_drop = downgrade.index('op.drop_constraint(published_run_fk, "tasks"')
    table_drop = downgrade.index('op.drop_table("task_qa_runs")')
    assert pointer_drop < table_drop
