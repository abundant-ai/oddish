from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "prod_schema_repair_001_reconcile_prompt_schema.py"
)


def test_repair_is_a_new_revision_after_the_current_release_head():
    source = MIGRATION.read_text()

    assert 'revision: str = "prod_schema_repair_001"' in source
    assert 'down_revision: Union[str, Sequence[str], None] = "analyzer_block_job_001"' in source


def test_repair_adds_each_task_version_column_independently():
    source = MIGRATION.read_text()

    for column in (
        "pre_trial",
        "pre_trial_status",
        "pre_trial_error",
        "pre_trial_started_at",
        "pre_trial_finished_at",
    ):
        assert f'if "{column}" not in task_version_columns:' in source


def test_repair_preserves_legacy_task_data_before_dropping_columns():
    source = MIGRATION.read_text()

    backfill = source.index("UPDATE task_versions")
    drop = source.index('op.drop_column("tasks", column)')

    assert backfill < drop
    assert "tasks.current_version_id = task_versions.id" in source
    assert "COALESCE(task_versions.{column}, tasks.{column})" in source
