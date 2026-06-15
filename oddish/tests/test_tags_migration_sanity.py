"""Static sanity checks for the tag-table migration.

We can't run real DDL in the unit-test container; instead we read the
migration file and assert that key SQL fragments are present. This catches
the easy regressions (typos in CREATE TABLE / index predicates) without
needing a Postgres instance.
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _read(name: str) -> str:
    return (MIGRATIONS / name).read_text()


def test_core_migration_creates_each_table():
    src = _read("aa00ta01core_add_tag_tables.py")
    for table in (
        "tags",
        "tag_assignments",
        "tag_exclusions",
        "tag_grants",
        "tag_events",
        "tag_policies",
        "saved_tag_filters",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in src, f"missing {table}"


def test_core_migration_creates_partial_unique_indexes():
    src = _read("aa00ta01core_add_tag_tables.py")
    assert "uq_tags_org_normalized" in src
    assert "uq_tag_assignments_target" in src
    assert "uq_tag_exclusions_target" in src
    assert "uq_tag_grants_principal" in src
    assert "uq_saved_tag_filters_owner_name" in src
    assert "deleted_at IS NULL AND state <> 'DELETED'" in src
    assert "COALESCE(org_id, '')" in src
    assert "COALESCE(principal_user_id, '')" in src


def test_core_migration_creates_enum_types():
    src = _read("aa00ta01core_add_tag_tables.py")
    for enum_name in (
        "tag_state",
        "tag_visibility",
        "tag_assignment_scope",
        "tag_assignment_state",
        "tag_assignment_source",
        "tag_grant_principal",
        "tag_grant_capability",
        "tag_event_action",
        "tag_event_actor",
        "tag_event_source",
        "saved_tag_filter_visibility",
        "tag_policy_who_can_create",
        "tag_policy_profanity_mode",
    ):
        assert f"CREATE TYPE {enum_name}" in src, f"missing enum {enum_name}"


def test_core_migration_revision_id():
    src = _read("aa00ta01core_add_tag_tables.py")
    assert 'revision: str = "aa00ta01core"' in src
    import re

    m = re.search(r'down_revision[^=]*=\s*"([^"]*)"', src)
    assert m is not None, "down_revision constant not found"
    assert m.group(1), "down_revision must not be empty"
    assert m.group(1) != "REPLACE_WITH_CURRENT_HEAD", (
        "the placeholder REPLACE_WITH_CURRENT_HEAD was never substituted; "
        "run `alembic heads` and use the real head id"
    )


def test_core_migration_creates_worker_jobs_coalescing_index():
    src = _read("aa00ta01core_add_tag_tables.py")
    assert "uq_worker_jobs_tag_project_active" in src
    assert "kind = 'TAG_PROJECT'" in src
    assert "status IN ('QUEUED', 'RETRYING')" in src


def test_projection_columns_migration():
    src = _read("aa01ta02proj_add_effective_tag_columns.py")
    assert 'revision: str = "aa01ta02proj"' in src
    assert '"aa00ta01core"' in src
    assert "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS effective_tag_ids" in src
    assert "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS current_version_tag_ids" in src
    assert "ALTER TABLE task_versions ADD COLUMN IF NOT EXISTS effective_tag_ids" in src
    assert "TEXT[] NOT NULL DEFAULT '{}'" in src


def test_gin_index_migration_uses_concurrently_and_autocommit():
    src = _read("aa02ta03gin_add_effective_tag_gin_indexes.py")
    assert 'revision: str = "aa02ta03gin"' in src
    assert '"aa01ta02proj"' in src
    assert "with op.get_context().autocommit_block():" in src
    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in src
    assert "USING GIN (effective_tag_ids array_ops)" in src
    assert "idx_tasks_effective_tag_ids_gin" in src
    assert "idx_task_versions_effective_tag_ids_gin" in src


def test_tag_project_enum_add_is_autocommit_and_not_referenced():
    src = _read("aa03ta04kind_add_tag_project_worker_kind.py")
    assert 'revision: str = "aa03ta04kind"' in src
    # The enum-add must run BEFORE aa00ta01core, which references 'TAG_PROJECT'
    # in its uq_worker_jobs_tag_project_active index -- a newly added enum
    # value can't be used until the transaction that added it has committed.
    assert '"trial_is_probe_001"' in src
    assert '"aa03ta04kind"' in _read("aa00ta01core_add_tag_tables.py")
    assert "with op.get_context().autocommit_block():" in src
    assert "ALTER TYPE worker_job_kind ADD VALUE IF NOT EXISTS 'TAG_PROJECT'" in src
    # The newly-added enum value MUST NOT be referenced in the
    # same migration — Postgres raises "unsafe use of new value".
    assert "INSERT" not in src.split("ADD VALUE", 1)[1]
    assert "kind = 'TAG_PROJECT'" not in src


def test_cloud_fk_migration_present():
    cloud_dir = Path(__file__).resolve().parents[2] / "backend" / "alembic" / "versions"
    src = (cloud_dir / "n0p1q2r3s4t5_add_tag_cloud_fks.py").read_text()
    assert 'revision: str = "n0p1q2r3s4t5"' in src
    assert '"m9n0p1q2r3s4"' in src
    # Each FK is added NOT VALID to avoid full-table validation locks; the
    # ADD CONSTRAINT statements are gated by IF NOT EXISTS via DO block.
    for table in (
        "tags",
        "tag_assignments",
        "tag_exclusions",
        "tag_grants",
        "tag_events",
        "saved_tag_filters",
        "tag_policies",
    ):
        assert table in src
    assert "REFERENCES organizations" in src
    assert "REFERENCES users" in src


def test_sweep_state_migration_present():
    src = _read("aa04ta05sweep_add_tag_sweep_state.py")
    assert 'revision: str = "aa04ta05sweep"' in src
    assert '"aa02ta03gin"' in src
    assert "CREATE TABLE IF NOT EXISTS tag_projection_sweep_state" in src
    assert "last_full_sweep_at" in src


# Tables whose CREATE TABLE column DEFAULTs we hold to model parity.
_TAG_TABLES = (
    "tags",
    "tag_assignments",
    "tag_exclusions",
    "tag_grants",
    "tag_events",
    "tag_policies",
    "saved_tag_filters",
)


def _migration_default_columns(src: str, table: str) -> list[str]:
    """Column names declared with a DEFAULT inside ``CREATE TABLE <table>``.

    The column bodies are indented 12 spaces and the closing ``)`` sits alone
    at 8 spaces, so we capture up to ``\\n        )`` -- inner parens like
    ``NOW()``, ``tags(id)`` and ``'{}'::jsonb`` never appear at that indent.
    """
    import re

    body = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n        \)",
        src,
        re.DOTALL,
    )
    assert body is not None, f"CREATE TABLE for {table!r} not found in migration"
    cols: list[str] = []
    for line in body.group(1).splitlines():
        m = re.match(r"\s*(\w+)\s+\S.*\bDEFAULT\b", line)
        if m:
            cols.append(m.group(1))
    return cols


def test_migration_table_defaults_have_model_server_defaults():
    """create_all / migration parity guard for tag-table column DEFAULTs.

    Fresh databases are bootstrapped by alembic ``000_initial_schema`` via
    ``Base.metadata.create_all()`` -- tables come from the MODELS, and the tag
    migrations' ``CREATE TABLE IF NOT EXISTS`` then no-op. So any column whose
    DEFAULT lives only in the migration DDL (and not as a model
    ``server_default``) silently loses its DB default on fresh databases. That
    crashed ``POST /tags`` in the deployed preview (NotNullViolation on
    ``tag_policies.updated_at``). This test pins every migration-declared
    DEFAULT to a non-None model ``server_default`` so the bug class can't
    silently return.
    """
    import oddish.db as db
    from oddish.db import Base
    from oddish.db.models import TimestampedMixin

    src = _read("aa00ta01core_add_tag_tables.py")

    # Resolve each tag table to its mapped class so we can tell which
    # created_at/updated_at columns are contributed by TimestampedMixin.
    model_by_table = {}
    for name in dir(db):
        obj = getattr(db, name, None)
        if getattr(obj, "__tablename__", None) in _TAG_TABLES:
            model_by_table[obj.__tablename__] = obj
    unresolved = sorted(set(_TAG_TABLES) - set(model_by_table))
    assert not unresolved, f"could not resolve models for tables: {unresolved}"

    # TimestampedMixin defines created_at/updated_at WITHOUT a server_default;
    # the tag cores always set those columns explicitly in their raw-SQL
    # INSERTs, so a missing DB default is harmless there. Exclude exactly those
    # mixin-provided columns. tag_events and tag_policies do NOT use the mixin,
    # so their timestamp columns (e.g. the fixed tag_policies.updated_at) are
    # still asserted.
    missing: list[str] = []
    checked = 0
    for table in _TAG_TABLES:
        from_mixin = issubclass(model_by_table[table], TimestampedMixin)
        columns = Base.metadata.tables[table].columns
        for col_name in _migration_default_columns(src, table):
            if col_name in ("created_at", "updated_at") and from_mixin:
                continue
            checked += 1
            if columns[col_name].server_default is None:
                missing.append(f"{table}.{col_name}")

    assert not missing, (
        "migration declares a column DEFAULT but the model column has no "
        "server_default -- create_all on a fresh DB would drop the default: "
        f"{missing}"
    )
    assert checked, "parser found no DEFAULT columns -- the regex likely broke"


def test_six_bugfixed_columns_keep_their_server_defaults():
    """Belt-and-suspenders pin for the six columns fixed by the production bug.

    Each must still be parsed from the migration DDL AND carry a non-None
    model ``server_default``, so a regex or column rename can't silently drop
    them from the parity loop above.
    """
    from oddish.db import Base

    src = _read("aa00ta01core_add_tag_tables.py")

    fixed = {
        "tag_policies": ("updated_at",),
        "tag_assignments": ("assigned_at",),
        "tag_events": ("actor_type", "source", "occurred_at"),
        "saved_tag_filters": ("filter_ast",),
    }
    for table, cols in fixed.items():
        declared = set(_migration_default_columns(src, table))
        for col in cols:
            assert (
                col in declared
            ), f"{table}.{col} is no longer parsed as a DEFAULT column"
            assert (
                Base.metadata.tables[table].columns[col].server_default is not None
            ), f"{table}.{col} lost its server_default (regression of the fix)"
