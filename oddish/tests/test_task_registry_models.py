from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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
