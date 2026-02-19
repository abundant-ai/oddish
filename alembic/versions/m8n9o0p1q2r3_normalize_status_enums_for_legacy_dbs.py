"""normalize_status_enums_for_legacy_dbs

Revision ID: m8n9o0p1q2r3
Revises: l7m8n9o0p1q2
Create Date: 2026-02-19 00:30:00.000000

Normalizes legacy enum drift:
- Convert trials.status from trialstatus -> jobstatus
- Ensure taskstatus includes newer pipeline states
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "m8n9o0p1q2r3"
down_revision: Union[str, Sequence[str], None] = "l7m8n9o0p1q2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # taskstatus historically had only {PENDING,RUNNING,SUCCESS,FAILED}.
    # Add states used by the current pipeline model.
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'ANALYZING'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'VERDICT_PENDING'")
    op.execute("ALTER TYPE taskstatus ADD VALUE IF NOT EXISTS 'COMPLETED'")

    # Convert trials.status to jobstatus if it's still trialstatus.
    op.execute(
        """
        DO $$
        DECLARE
            status_type text;
        BEGIN
            SELECT c.udt_name INTO status_type
            FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.table_name = 'trials'
              AND c.column_name = 'status';

            IF status_type = 'trialstatus' THEN
                EXECUTE 'DROP INDEX IF EXISTS idx_trials_claimable';
                EXECUTE 'ALTER TABLE trials ALTER COLUMN status DROP DEFAULT';
                EXECUTE 'ALTER TABLE trials ALTER COLUMN status TYPE jobstatus USING status::text::jobstatus';
                EXECUTE 'ALTER TABLE trials ALTER COLUMN status SET DEFAULT ''PENDING''::jobstatus';
                EXECUTE $sql$
                    CREATE INDEX IF NOT EXISTS idx_trials_claimable
                    ON trials (status, provider, next_retry_at)
                    WHERE status = ANY (ARRAY['PENDING'::jobstatus, 'RETRYING'::jobstatus])
                $sql$;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    """Downgrade schema.

    Not safely reversible for mixed legacy/modern databases.
    """
    pass
