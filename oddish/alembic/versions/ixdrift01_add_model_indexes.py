"""add the model-declared single-column indexes prod never received

The SQLAlchemy models declare these via ``index=True`` (plus two unique
ones), so every create_all-built preview had them -- but no migration ever
created them on prod. Surfaced by the preview snapshot rehearsal's
model-schema assert (PR #585): prod runs without indexes on hot filters
like ``trials.org_id`` and ``trials.experiment_id``.

Each step runs in its own autocommit transaction with CREATE INDEX
CONCURRENTLY (never blocks live DML) and a lock_timeout so a contended
step fails fast; every step is idempotent and safe to re-trigger. A
previously failed CONCURRENTLY attempt leaves an INVALID index behind --
those are dropped before recreation. Tables created by the backend stack
(quotas, and the cloud columns on users/organizations) are guarded with
to_regclass so a from-scratch oddish-only upgrade skips them until the
backend chain creates them.
"""

from alembic import op
import sqlalchemy as sa

revision = "ixdrift01_add_model_indexes"
down_revision = "skip01_add_skipped"
branch_labels = None
depends_on = None

INDEXES = (
    # (index name, table, column, unique)
    ("ix_experiments_org_id", "experiments", "org_id", False),
    ("ix_tasks_org_id", "tasks", "org_id", False),
    ("ix_trials_org_id", "trials", "org_id", False),
    ("ix_trials_queue_key", "trials", "queue_key", False),
    ("ix_trials_experiment_id", "trials", "experiment_id", False),
    ("ix_trials_idempotency_key", "trials", "idempotency_key", True),
    ("ix_tags_org_id", "tags", "org_id", False),
    ("ix_tag_assignments_org_id", "tag_assignments", "org_id", False),
    ("ix_tag_exclusions_org_id", "tag_exclusions", "org_id", False),
    ("ix_tag_grants_org_id", "tag_grants", "org_id", False),
    ("ix_organizations_clerk_org_id", "organizations", "clerk_org_id", True),
    ("ix_users_clerk_user_id", "users", "clerk_user_id", False),
    ("ix_quotas_org_id", "quotas", "org_id", False),
    ("ix_quotas_user_id", "quotas", "user_id", False),
)


def _autocommit(sql: str) -> None:
    with op.get_context().autocommit_block():
        op.execute(sql)


def _table_exists(table: str) -> bool:
    bind = op.get_bind()
    return (
        bind.execute(
            sa.text("SELECT to_regclass(:qualified)"),
            {"qualified": f"public.{table}"},
        ).scalar()
        is not None
    )


def _drop_invalid(name: str) -> None:
    """Clear the INVALID leftover of a previously failed CONCURRENTLY build."""
    bind = op.get_bind()
    invalid = bind.execute(
        sa.text(
            """
            SELECT 1
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relname = :name AND NOT i.indisvalid
            """
        ),
        {"name": name},
    ).scalar()
    if invalid:
        _autocommit(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')


def upgrade() -> None:
    # Session-level; persists across the autocommit steps on this connection.
    # CONCURRENTLY's lock waits (ShareUpdateExclusive + old-snapshot virtual
    # xid waits) never block DML, so waiting patiently is safe -- prod's first
    # run showed 8s systematically loses the race against routine worker
    # transactions on experiments/trials (both the CREATE and the INVALID-
    # leftover DROP timed out). statement_timeout is cleared so a slow wait
    # or build is not killed midway.
    _autocommit("SET lock_timeout = '300s'")
    _autocommit("SET statement_timeout = '0'")
    for name, table, column, unique in INDEXES:
        if not _table_exists(table):
            continue
        _drop_invalid(name)
        uq = "UNIQUE " if unique else ""
        _autocommit(
            f'CREATE {uq}INDEX CONCURRENTLY IF NOT EXISTS "{name}" '
            f"ON {table} ({column})"
        )


def downgrade() -> None:
    _autocommit("SET lock_timeout = '300s'")
    _autocommit("SET statement_timeout = '0'")
    for name, _table, _column, _unique in INDEXES:
        _autocommit(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
