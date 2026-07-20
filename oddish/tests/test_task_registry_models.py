from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.types import Text

from oddish.db.models import MetadataSource, TaskModel, TaskVersionModel


def test_task_model_has_descriptive_metadata_columns():
    columns = TaskModel.__table__.columns
    for name in (
        "description",
        "category",
        "category_raw",
        "author_name",
        "author_email",
        "author_organization",
        "expert_time_hours",
        "metadata_source",
        "metadata_updated_at",
    ):
        assert name in columns, f"TaskModel missing column {name}"
        assert columns[name].nullable, f"{name} must be nullable for old-CLI uploads"


def test_metadata_source_enum_values():
    assert [m.value for m in MetadataSource] == ["CLIENT", "BACKFILL"]


def test_task_version_model_has_runtime_and_snapshot_columns():
    columns = TaskVersionModel.__table__.columns
    runtime = (
        "allow_internet",
        "cpus",
        "memory_mb",
        "storage_mb",
        "gpus",
        "gpu_types",
        "agent_timeout_sec",
        "verifier_timeout_sec",
        "build_timeout_sec",
    )
    snapshot = (
        "description_snapshot",
        "category_snapshot",
        "topic_tags_snapshot",
        "expert_time_hours_snapshot",
    )
    for name in runtime + snapshot:
        assert name in columns, f"TaskVersionModel missing column {name}"
        assert columns[name].nullable, f"{name} must be nullable"

    assert isinstance(columns["gpu_types"].type, ARRAY)
    assert isinstance(columns["topic_tags_snapshot"].type, ARRAY)

    task_columns = TaskModel.__table__.columns

    snapshot_numeric = columns["expert_time_hours_snapshot"].type
    task_numeric = task_columns["expert_time_hours"].type
    assert snapshot_numeric.precision == task_numeric.precision
    assert snapshot_numeric.scale == task_numeric.scale

    assert columns["category_snapshot"].type.length == task_columns["category"].type.length

    assert isinstance(columns["description_snapshot"].type, Text)
    assert isinstance(task_columns["description"].type, Text)


def test_task_upload_event_model_shape():
    from oddish.db.models import TaskUploadEventModel

    columns = TaskUploadEventModel.__table__.columns
    assert TaskUploadEventModel.__tablename__ == "task_upload_events"
    # task_version_id is NULL for content-unchanged re-uploads, which is the
    # whole reason this table exists separately from task_versions.
    assert columns["task_version_id"].nullable
    assert not columns["task_id"].nullable
    # created_version must be NOT NULL: it's the only thing that survives
    # ON DELETE SET NULL to distinguish a real version-creating upload from
    # a content-unchanged no-op.
    assert "created_version" in columns, "TaskUploadEventModel missing column created_version"
    assert not columns["created_version"].nullable
    for name in (
        "content_hash",
        "source_repo",
        "source_commit",
        "source_ref",
        "source_path",
        "ci_provider",
        "ci_run_id",
        "ci_run_url",
        "ci_pr_number",
        "uploader_is_ci",
        "uploader_user_id",
        "uploader_cli_version",
        "uploader_host",
    ):
        assert name in columns, f"TaskUploadEventModel missing column {name}"
