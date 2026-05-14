"""make circular foreign keys deferrable

``pg_dump`` warns whenever the schema contains a foreign-key cycle
because it has no row order that satisfies the constraints during a
data-only restore. The three offenders in this schema are:

- ``tasks.current_version_id`` -> ``task_versions.id``
  (paired with ``task_versions.task_id`` -> ``tasks.id``; a true
  two-table cycle)
- ``trials.superseded_by_trial_id`` -> ``trials.id``
  (self-referential)
- ``worker_jobs.parent_job_id`` -> ``worker_jobs.id``
  (self-referential)

A data-only ``pg_restore`` of an oddish dump fails on the first cycle
with::

    pg_restore: error: COPY failed for table "tasks":
    ERROR:  insert or update on table "tasks" violates foreign key
    constraint "fk_tasks_current_version_id"
    DETAIL:  Key (current_version_id)=(...) is not present in table
    "task_versions".

Marking each FK ``DEFERRABLE INITIALLY DEFERRED`` lets the constraint
check slide to commit time, so a restore can ``COPY`` rows in any
order and the cycle closes itself before the transaction commits.
Runtime semantics are otherwise unchanged: ordinary single-row
autocommit traffic still observes the constraint at the same moment
it did before (commit of the implicit txn).

Lookup the constraint by ``(table, column)`` instead of hardcoding
the name because the three FKs were created via different paths
(explicit ``create_foreign_key`` name, inline ``REFERENCES`` with a
PG-auto-generated name) and may have drifted across deployments.

Revision ID: i0d1e2f3a4b5
Revises: h9c0d1e2f3a4
Create Date: 2026-05-14 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "i0d1e2f3a4b5"
down_revision: Union[str, Sequence[str], None] = "h9c0d1e2f3a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_CIRCULAR_FKS: tuple[tuple[str, str], ...] = (
    ("tasks", "current_version_id"),
    ("trials", "superseded_by_trial_id"),
    ("worker_jobs", "parent_job_id"),
)


def _alter_deferrable(clause: str) -> None:
    for table, column in _CIRCULAR_FKS:
        op.execute(
            f"""
            DO $$
            DECLARE
                fk_name TEXT;
            BEGIN
                SELECT c.conname INTO fk_name
                FROM pg_constraint c
                JOIN pg_class t ON t.oid = c.conrelid
                JOIN pg_namespace n ON n.oid = t.relnamespace
                JOIN pg_attribute a
                    ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey)
                WHERE c.contype = 'f'
                  AND n.nspname = 'public'
                  AND t.relname = '{table}'
                  AND a.attname = '{column}'
                  AND array_length(c.conkey, 1) = 1
                LIMIT 1;

                IF fk_name IS NOT NULL THEN
                    EXECUTE format(
                        'ALTER TABLE %I ALTER CONSTRAINT %I {clause}',
                        '{table}', fk_name
                    );
                END IF;
            END
            $$;
            """
        )


def upgrade() -> None:
    _alter_deferrable("DEFERRABLE INITIALLY DEFERRED")


def downgrade() -> None:
    _alter_deferrable("NOT DEFERRABLE INITIALLY IMMEDIATE")
