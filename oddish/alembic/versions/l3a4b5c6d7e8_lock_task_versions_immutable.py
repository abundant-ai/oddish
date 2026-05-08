"""enforce task_versions write-once at the database layer

P6 of the task-first migration. P0 dropped ``updated_at`` from
``task_versions`` to make the intent obvious in the schema, but that
was theatre -- a future code path could still issue an UPDATE. This
migration adds a real guard: a row-level trigger that raises on any
UPDATE to ``task_versions`` except for the soft-delete column
``deleted_at``.

Rationale:

- Once a task version is written, its bundle, content_hash, version
  number, and authorship are part of the evidence pool. Mutating any
  of those would invalidate every trial pinned to that version.
- ``deleted_at`` is the one legitimate post-create write (soft-delete
  retention housekeeping); allowing it preserves the existing cleanup
  pathways without compromising the invariant.

If you legitimately need to update a non-``deleted_at`` field (e.g. a
backfill of ``expanded_manifest_key`` from an out-of-band re-expand),
disable the trigger for the duration of the migration:

    ALTER TABLE task_versions DISABLE TRIGGER task_versions_immutable_guard;
    -- ... do the update ...
    ALTER TABLE task_versions ENABLE TRIGGER task_versions_immutable_guard;

Revision ID: l3a4b5c6d7e8
Revises: k2f3a4b5c6d7
Create Date: 2026-05-01 02:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "l3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "j1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION task_versions_block_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            -- Block mutation of the *immutable* fields. The expansion
            -- columns (``expanded_at``, ``expanded_manifest_key``) and
            -- the soft-delete column (``deleted_at``) are post-create
            -- writes by design and are deliberately allowed.
            IF (
                NEW.id                    IS DISTINCT FROM OLD.id
                OR NEW.task_id            IS DISTINCT FROM OLD.task_id
                OR NEW.version            IS DISTINCT FROM OLD.version
                OR NEW.task_path          IS DISTINCT FROM OLD.task_path
                OR NEW.task_s3_key        IS DISTINCT FROM OLD.task_s3_key
                OR NEW.content_hash       IS DISTINCT FROM OLD.content_hash
                OR NEW.message            IS DISTINCT FROM OLD.message
                OR NEW.created_by_user_id IS DISTINCT FROM OLD.created_by_user_id
                OR NEW.created_at         IS DISTINCT FROM OLD.created_at
            ) THEN
                RAISE EXCEPTION
                    'task_versions is write-once: mutation of % blocked',
                    NEW.id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS task_versions_immutable_guard ON task_versions
        """
    )
    op.execute(
        """
        CREATE TRIGGER task_versions_immutable_guard
            BEFORE UPDATE ON task_versions
            FOR EACH ROW
            EXECUTE FUNCTION task_versions_block_mutation()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS task_versions_immutable_guard ON task_versions"
    )
    op.execute("DROP FUNCTION IF EXISTS task_versions_block_mutation()")
