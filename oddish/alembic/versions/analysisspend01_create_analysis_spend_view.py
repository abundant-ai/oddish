"""Create the analysis_spend view -- the frozen-ledger seam, in one place.

Revision ID: analysisspend01
Revises: clearstale01
Create Date: 2026-08-18

Pre-cutover analysis spend lives in the append-only ``analysis_costs``
ledger (frozen at cutover; nothing writes it anymore). Post-cutover spend
sits on the QA/audit trial rows themselves (``trials.cost_usd`` where
``kind != 'agent'``). Every time-series QA-cost surface reads this view so
no dashboard implements the union itself -- if the ledger is ever archived,
only this view changes.

Column notes against the real shapes: ``analysis_costs`` has no
``occurred_at`` column, so its event time is ``created_at``. A trial's
event time is ``finished_at``, falling back to ``updated_at`` for a
settled row that never stamped one. Both arms filter soft-deleted rows
(``deleted_at`` comes from TimestampedMixin on both tables).

``CREATE OR REPLACE VIEW`` is idempotent. Fresh (``create_all``) databases
get the same view from the ``after_create`` listener in ``oddish.db.models``
(``create_all`` never creates views on its own); a test pins the two
definitions identical.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "analysisspend01"
down_revision: Union[str, Sequence[str], None] = "clearstale01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE VIEW analysis_spend AS
          SELECT created_at              AS occurred_at,
                 org_id,
                 task_id,
                 trial_id,
                 billed_user_id,
                 job_kind                AS kind,
                 model,
                 cost_usd,
                 'analysis_costs'::text  AS source
            FROM analysis_costs
           WHERE deleted_at IS NULL
        UNION ALL
          SELECT COALESCE(t.finished_at, t.updated_at) AS occurred_at,
                 t.org_id,
                 t.task_id,
                 t.id                    AS trial_id,
                 t.billed_user_id,
                 t.kind,
                 t.model,
                 t.cost_usd,
                 'trials'::text          AS source
            FROM trials t
           WHERE t.kind != 'agent'
             AND t.cost_usd IS NOT NULL
             AND t.deleted_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS analysis_spend")
