"""Assert the ORM models and the migration-built schema agree on the task
registry tables.

Requires the configured database to already be migrated to head (e.g. via
``alembic upgrade head`` -- see ``migration-upgrade-check.yml``, which does
the same in CI). This is a read-only comparison; it does not migrate.

Scoped to ``tasks`` / ``task_versions`` / ``task_upload_events`` -- the
tables taskreg_001..004 own -- rather than the whole schema. A whole-schema
``compare_metadata`` run reports substantial pre-existing drift from older,
unrelated parts of this large schema (GIN/partial/functional indexes that
predate this feature and were never mirrored into the ORM); asserting on
all of it would make this test permanently red for reasons no taskreg
change could fix. Scoping keeps the assertion honest while still catching
the two defect classes that have actually bitten this repo in production:
a column or index declared on one side (ORM or migration) and missing on
the other -- see ``ixdrift01_add_model_indexes.py`` and commit 0ee345ad
(``index=True`` on ``TaskModel.category`` duplicating the migration's
``idx_tasks_category`` partial index, caught by an ad-hoc version of this
check during taskreg development).

Even scoped to these three tables, a few index-level diffs are expected and
not bugs -- see ``_KNOWN_PRE_EXISTING_DRIFT`` below for what they are and
why. Anything else is a real failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

import oddish.db.connection as _conn
from oddish.db.models import Base

_SCOPED_TABLES = {"tasks", "task_versions", "task_upload_events"}

# (diff_kind, table_name, index_name) triples that are known, intentional
# asymmetries -- not regressions:
#
# - idx_task_upload_events_task_created: built CONCURRENTLY by taskreg_003
#   and deliberately left off the ORM to avoid model/migration index-name
#   drift (see the comment on TaskUploadEventModel in models.py).
# - idx_tasks_category: taskreg_001's partial index (``WHERE category IS
#   NOT NULL``); commit 0ee345ad removed the duplicate ``index=True`` on
#   TaskModel.category rather than re-declaring the partial index in the
#   ORM, so this asymmetry is the fix, not a bug.
# - idx_task_versions_effective_tag_ids_gin / idx_tasks_effective_tag_ids_gin:
#   GIN(array_ops) indexes from aa02ta03gin, predates taskreg, not
#   expressible via a plain SQLAlchemy Index().
# - idx_tasks_imported_at: partial index from legacy_imported_at_001
#   (Sauron migration), predates taskreg.
# - idx_tasks_org_id: legacy hand-named index from
#   b2c3d4e5f6a7_add_cloud_ready_columns; TaskModel.org_id's ``index=True``
#   covers the same column under SQLAlchemy's ``ix_tasks_org_id`` naming, so
#   this is a harmless pre-existing duplicate, not something taskreg touches.
# - idx_tasks_org_lower_github_tag_live (both directions): a functional
#   index comparison false positive -- Postgres reflects the compiled
#   expression with an explicit ``::text`` cast that the declared
#   ``postgresql_where``/``text()`` clause doesn't have. Same index, same
#   predicate.

# Keyed on the index's actual definition (expressions + partial-index WHERE),
# not just its name -- a same-named index whose definition changed (columns
# swapped, predicate altered, uniqueness flipped) must NOT match one of
# these entries silently. That's also why idx_tasks_org_lower_github_tag_live
# needs both directions listed with their differing expression text: the
# remove_index (reflected) and add_index (declared) sides are the SAME index
# but Postgres echoes the compiled expression with an explicit ``::text``
# cast the declared ``text()`` clause lacks, so they don't share a
# signature. Get real signatures by printing ``_diff_key(d)`` for each diff.
_KNOWN_PRE_EXISTING_DRIFT: set[tuple] = {
    (
        "remove_index",
        "task_upload_events",
        "idx_task_upload_events_task_created",
        False,
        ("task_upload_events.task_id", "task_upload_events.created_at DESC"),
        None,
    ),
    (
        "remove_index",
        "tasks",
        "idx_tasks_category",
        False,
        ("tasks.category",),
        "(category IS NOT NULL)",
    ),
    (
        "remove_index",
        "task_versions",
        "idx_task_versions_effective_tag_ids_gin",
        False,
        ("task_versions.effective_tag_ids",),
        None,
    ),
    (
        "remove_index",
        "tasks",
        "idx_tasks_effective_tag_ids_gin",
        False,
        ("tasks.effective_tag_ids",),
        None,
    ),
    (
        "remove_index",
        "tasks",
        "idx_tasks_imported_at",
        False,
        ("tasks.imported_at",),
        "(imported_at IS NOT NULL)",
    ),
    (
        "remove_index",
        "tasks",
        "idx_tasks_org_id",
        False,
        ("tasks.org_id",),
        None,
    ),
    (
        "remove_index",
        "tasks",
        "idx_tasks_org_lower_github_tag_live",
        False,
        ("tasks.org_id", "lower(tags ->> 'github_username'::text)"),
        "(deleted_at IS NULL)",
    ),
    (
        "add_index",
        "tasks",
        "idx_tasks_org_lower_github_tag_live",
        False,
        ("tasks.org_id", "lower((tags ->> 'github_username'))"),
        "deleted_at IS NULL",
    ),
}


def _include_object(object_, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in _SCOPED_TABLES
    table = getattr(object_, "table", None)
    return table is not None and table.name in _SCOPED_TABLES


def _diff_key(diff) -> tuple | None:
    kind = diff[0]
    if kind not in ("add_index", "remove_index"):
        return None
    idx = diff[1]
    where = idx.dialect_options.get("postgresql", {}).get("where")
    return (
        kind,
        idx.table.name,
        idx.name,
        idx.unique,
        tuple(str(e) for e in idx.expressions),
        str(where) if where is not None else None,
    )


def _compare(connection):
    context = MigrationContext.configure(
        connection,
        opts={"compare_type": True, "include_object": _include_object},
    )
    return compare_metadata(context, Base.metadata)


@pytest.mark.asyncio
async def test_orm_matches_migrated_schema_for_task_registry_tables():
    async with _conn.engine.connect() as connection:
        diffs = await connection.run_sync(_compare)

    unexpected = [d for d in diffs if _diff_key(d) not in _KNOWN_PRE_EXISTING_DRIFT]
    assert unexpected == [], (
        "ORM models and migration-built schema disagree on "
        "tasks/task_versions/task_upload_events beyond the known "
        f"pre-existing baseline: {unexpected}"
    )
