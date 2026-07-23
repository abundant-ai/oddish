from pathlib import Path


def test_prompts_003_backfills_pre_trial_before_dropping_task_columns():
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "prompts_003_prompt_kind_latest_wins.py"
    ).read_text()

    backfill = migration.index("UPDATE task_versions")
    drop = migration.index('op.drop_column("tasks", col)')

    assert backfill < drop
    assert "tasks.current_version_id = task_versions.id" in migration
    assert "COALESCE(task_versions.{col}, tasks.{col})" in migration
