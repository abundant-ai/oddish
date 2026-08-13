"""Rename cohort_comparison analyzer blocks to agent_capabilities.

Revision ID: agentcap01
Revises: task_browse_summary_002

The analyzer was named for its input (two cohorts of trials) rather than its
output (what the agent can and cannot do). ``AnalyzerType.COHORT_COMPARISON``
became ``AGENT_CAPABILITIES``, and ``analyzer_blocks.type`` stores that enum's
``.value`` as a plain string, so stored rows have to move with it.

Data-only and idempotent -- re-running matches nothing the second time, and a
fresh database updates zero rows. ``api.services.agent_capabilities`` reads
both spellings (``LEGACY_BLOCK_TYPE``) for one release, so neither ordering of
this migration against the code deploy loses a stored analysis, and a rollback
still resolves the rows this renamed.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "agentcap01"
down_revision: Union[str, Sequence[str], None] = "task_browse_summary_002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # analyzer_blocks takes writes from live analysis jobs. This is one
    # statement against one table, so it cannot invert a lock order, but it
    # can still queue behind a long writer -- fail fast instead of holding the
    # deploy's migration step open (prod applies these with no default
    # lock_timeout).
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        UPDATE analyzer_blocks
        SET type = 'agent_capabilities'
        WHERE type = 'cohort_comparison'
        """
    )


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(
        """
        UPDATE analyzer_blocks
        SET type = 'cohort_comparison'
        WHERE type = 'agent_capabilities'
        """
    )
