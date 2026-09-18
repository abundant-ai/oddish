"""Persist container ownership and outcomes independently of later retries."""

from alembic import op
import sqlalchemy as sa

revision = "worker_interruptions_001"
down_revision = "delivery_history_001"
branch_labels = None
depends_on = None

# A DB trigger covers cancellation and heartbeat cleanup as well as ordinary
# outcomes. The attempt and scheduling transition commit or roll back together.
ATTEMPT_OUTCOME_SQL = """
CREATE OR REPLACE FUNCTION record_worker_attempt_outcome() RETURNS trigger AS $$
BEGIN
    IF OLD.status::text = 'RUNNING' AND NEW.status::text <> 'RUNNING' THEN
        UPDATE worker_resource_attempts
        SET outcome = CASE WHEN NEW.stale_reaped_at IS DISTINCT FROM OLD.stale_reaped_at
                           THEN 'INTERRUPTED' ELSE NEW.status::text END,
            interruption_reason = NEW.error_message,
            finished_at = CASE WHEN NEW.stale_reaped_at IS DISTINCT FROM OLD.stale_reaped_at
                               THEN NULL ELSE NOW() END
        WHERE worker_job_id = OLD.id AND attempt = OLD.attempts AND outcome IS NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER worker_attempt_outcome AFTER UPDATE ON worker_jobs
FOR EACH ROW EXECUTE FUNCTION record_worker_attempt_outcome();
"""


def upgrade():
    for name in (
        "worker_id",
        "modal_container_id",
        "reservation_token",
        "outcome",
        "interruption_reason",
        "sandbox_provider",
        "sandbox_external_id",
    ):
        op.add_column("worker_resource_attempts", sa.Column(name, sa.Text()))
    op.add_column(
        "worker_resource_attempts", sa.Column("finished_at", sa.DateTime(timezone=True))
    )
    op.add_column(
        "worker_resource_attempts",
        sa.Column(
            "cleanup_pending", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.create_index(
        "ix_worker_attempt_reservation",
        "worker_resource_attempts",
        ["reservation_token"],
    )
    op.create_index(
        "ix_worker_attempt_call", "worker_resource_attempts", ["modal_function_call_id"]
    )
    # Separate statements for asyncpg's prepared-statement protocol.
    function, trigger = ATTEMPT_OUTCOME_SQL.split("CREATE TRIGGER", 1)
    op.execute(function)
    op.execute("CREATE TRIGGER" + trigger)


def downgrade():
    op.execute("DROP TRIGGER worker_attempt_outcome ON worker_jobs")
    op.execute("DROP FUNCTION record_worker_attempt_outcome()")
    op.drop_index("ix_worker_attempt_call", table_name="worker_resource_attempts")
    op.drop_index(
        "ix_worker_attempt_reservation", table_name="worker_resource_attempts"
    )
    for name in (
        "worker_id",
        "modal_container_id",
        "reservation_token",
        "outcome",
        "interruption_reason",
        "sandbox_provider",
        "sandbox_external_id",
        "finished_at",
        "cleanup_pending",
    ):
        op.drop_column("worker_resource_attempts", name)
