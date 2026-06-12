"""Sanity that the hot tag-time-window composite index exists."""
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def test_hot_composite_index_present_in_core_migration():
    src = (MIGRATIONS / "aa00ta01core_add_tag_tables.py").read_text()
    assert "idx_tag_assignments_tag_org_created_target" in src
    assert "tag_assignments" in src
    assert "created_at DESC" in src
    assert "(tag_id, org_id, created_at DESC, target_id)" in src.replace(
        "  ", " "
    ) or all(
        token in src
        for token in ["tag_id", "org_id", "created_at DESC", "target_id"]
    )
