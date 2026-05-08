"""backfill jobs and trial agent_equivalence_key

Data migration paired with ``g8b9c0d1e2f3_add_jobs_and_agent_equivalence``.

Steps:

1. Ensure ``pgcrypto`` is available so ``digest()`` works for the
   equivalence-key hash. We use the same canonicalization as the Python
   helper ``compute_agent_equivalence_key``: lowercase, strip whitespace,
   join with '|'.

2. Create one synthetic Job per existing experiment, ``kind=ad_hoc``.
   Idempotent on ``triggered_by_experiment_id`` so re-running is a no-op.

3. Backfill ``trials.job_id`` from the experiment->job map for any trial
   that doesn't already have one.

4. Backfill ``trials.agent_equivalence_key`` from
   ``(agent, model, provider)`` for any trial that doesn't already have
   one. Skips terminal IMPORTED trials with a missing agent (shouldn't
   happen, but guard anyway).

This migration is data-only -- the schema is the previous revision's
job. Splitting them lets a future operator re-run just the backfill if
needed without touching DDL.

Revision ID: h9c0d1e2f3a4
Revises: g8b9c0d1e2f3
Create Date: 2026-05-01 00:45:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "h9c0d1e2f3a4"
down_revision: Union[str, Sequence[str], None] = "g8b9c0d1e2f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Postgres expression that mirrors compute_agent_equivalence_key in
# oddish.core.agent_identity. Kept inline so the SQL is self-contained.
_AGENT_KEY_SQL = """
encode(
    digest(
        LOWER(TRIM(COALESCE(agent, ''))) || '|' ||
        LOWER(TRIM(COALESCE(model, ''))) || '|' ||
        LOWER(TRIM(COALESCE(provider, ''))),
        'sha256'
    ),
    'hex'
)
"""


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # 1. One synthetic Job per existing experiment.
    op.execute(
        """
        INSERT INTO jobs (
            id, kind, name, triggered_by_experiment_id,
            launched_by_user_id, launched_at, finished_at, org_id,
            created_at, updated_at
        )
        SELECT
            substr(md5(e.id || '|backfill'), 1, 16) AS id,
            'ad_hoc' AS kind,
            e.name AS name,
            e.id AS triggered_by_experiment_id,
            NULL AS launched_by_user_id,
            e.created_at AS launched_at,
            NULL AS finished_at,
            e.org_id AS org_id,
            e.created_at AS created_at,
            e.created_at AS updated_at
        FROM experiments e
        WHERE NOT EXISTS (
            SELECT 1 FROM jobs j
            WHERE j.triggered_by_experiment_id = e.id
        )
        """
    )

    # 2. Point each trial at its experiment's synthetic Job.
    op.execute(
        """
        UPDATE trials t
        SET job_id = j.id
        FROM jobs j
        WHERE j.triggered_by_experiment_id = t.experiment_id
          AND t.job_id IS NULL
        """
    )

    # 3. Backfill agent_equivalence_key from (agent, model, provider).
    op.execute(
        f"""
        UPDATE trials
        SET agent_equivalence_key = {_AGENT_KEY_SQL}
        WHERE agent_equivalence_key IS NULL
          AND agent IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("UPDATE trials SET agent_equivalence_key = NULL")
    op.execute("UPDATE trials SET job_id = NULL")
    op.execute(
        "DELETE FROM jobs WHERE triggered_by_experiment_id IS NOT NULL"
    )
