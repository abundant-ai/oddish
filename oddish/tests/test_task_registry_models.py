from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db.models import MetadataSource, TaskModel


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
