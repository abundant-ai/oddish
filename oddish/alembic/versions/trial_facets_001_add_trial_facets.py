"""add_trial_facets

Vocabulary table behind the task-browser filter dropdowns. The facets
endpoint used to recompute the distinct agent/model/provider/environment/
stage/classification values from the full ``trials`` table on every request
(seven serial DISTINCT scans — ~57s TTFB on staging-scale orgs). The
vocabulary is a few hundred rows per org, so it is stored instead:
write-through on trial creation, wholesale rebuild by a periodic sweep
(``oddish.core.trial_facets``), one indexed read on the request path.

The backfill below is those seven scans run one final time, off the request
path. Raw SQL bypasses the ORM soft-delete listener, so the ``deleted_at``
filters are explicit. Idempotent: re-running upserts nothing new.

Revision ID: trial_facets_001
Revises: trajgraph_002
Create Date: 2026-08-06 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "trial_facets_001"
down_revision: Union[str, Sequence[str], None] = "trajgraph_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The trial population the browse filters match: live, non-probe,
# non-superseded, on the task's current version, org-scoped.
_LIVE_TRIALS = """
    FROM trials t
    JOIN tasks k ON k.id = t.task_id
    WHERE t.org_id IS NOT NULL
      AND t.deleted_at IS NULL
      AND k.deleted_at IS NULL
      AND t.is_probe IS NOT TRUE
      AND t.superseded_by_trial_id IS NULL
      AND t.task_version_id = k.current_version_id
"""

_SCALAR_KINDS = (
    ("agent", "t.agent"),
    ("model", "t.model"),
    ("provider", "t.provider"),
    ("environment", "t.environment"),
    ("harbor_stage", "t.harbor_stage"),
    ("analysis_classification", "t.analysis->>'classification'"),
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS trial_facets (
            org_id VARCHAR(64) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            value VARCHAR(160) NOT NULL,
            value_2 VARCHAR(160) NOT NULL DEFAULT '',
            PRIMARY KEY (org_id, kind, value, value_2)
        )
        """
    )
    for kind, expr in _SCALAR_KINDS:
        op.execute(
            f"""
            INSERT INTO trial_facets (org_id, kind, value, value_2)
            SELECT DISTINCT t.org_id, '{kind}', LEFT({expr}, 160), ''
            {_LIVE_TRIALS}
              AND {expr} IS NOT NULL
            ON CONFLICT DO NOTHING
            """
        )
    op.execute(
        f"""
        INSERT INTO trial_facets (org_id, kind, value, value_2)
        SELECT DISTINCT t.org_id, 'agent_model',
               LEFT(t.agent, 160), COALESCE(LEFT(t.model, 160), '')
        {_LIVE_TRIALS}
          AND t.agent IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS trial_facets")
