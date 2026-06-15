"""normalize nop/oracle baselines to a single nop_oracle model + queue id

The earlier ``637c45e5ac80`` migration only moved the exact ``nop`` / ``oracle``
agents onto the dedicated ``nop_oracle`` queue, and left their model as the
separate ``default`` string. Baseline variants people use in practice
(``oracle-v2``, ``agent-nop``, ``nop-foo``, ...) slipped through entirely, so
they kept whatever model string the caller passed (``default``, ``nop_oracle``,
or arbitrary junk) and were routed by that string. This collapses every
baseline trial -- matching the same set the dashboard's ``_baseline_agent_clause``
/ config's ``is_nop_oracle_agent`` recognize -- onto a single canonical
``nop_oracle`` id for BOTH the model and the queue key, so the stored model, the
queue key, and the concurrency bucket all agree. It also mirrors the queue key
onto their still-pending ``worker_jobs`` rows.

Revision ID: nopx01_nop_oracle_variants
Revises: qh01_runtime_status
Create Date: 2026-06-15 14:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "nopx01_nop_oracle_variants"
down_revision: Union[str, Sequence[str], None] = "qh01_runtime_status"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mirrors oddish.config.is_nop_oracle_agent / dashboard._baseline_agent_clause.
def _baseline_clause(col: str) -> str:
    return f"""
        LOWER(COALESCE({col}, '')) IN ('nop', 'oracle')
        OR LOWER(COALESCE({col}, '')) LIKE 'nop-%'
        OR LOWER(COALESCE({col}, '')) LIKE 'oracle-%'
        OR LOWER(COALESCE({col}, '')) LIKE 'agent-nop%'
        OR LOWER(COALESCE({col}, '')) LIKE 'agent-oracle%'
    """


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE trials
        SET queue_key = 'nop_oracle',
            model = 'nop_oracle'
        WHERE ({_baseline_clause("agent")})
          AND (queue_key IS DISTINCT FROM 'nop_oracle'
               OR model IS DISTINCT FROM 'nop_oracle')
        """
    )
    op.execute(
        f"""
        UPDATE worker_jobs AS wj
        SET queue_key = 'nop_oracle'
        FROM trials AS t
        WHERE wj.kind = 'TRIAL'
          AND wj.subject_table = 'trials'
          AND wj.subject_id = t.id
          AND ({_baseline_clause("t.agent")})
          AND wj.status IN ('QUEUED', 'RETRYING', 'BLOCKED')
          AND wj.queue_key IS DISTINCT FROM 'nop_oracle'
        """
    )


def downgrade() -> None:
    # No-op: the prior model strings were arbitrary and unrecoverable, and the
    # canonical default/nop_oracle assignment is the intended steady state.
    pass
