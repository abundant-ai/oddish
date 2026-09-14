"""Persist endpoint check ownership, observations, and incident history."""

from alembic import op

revision = "endpoint_health_001"
down_revision = "org_execution_001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in """
        CREATE TABLE endpoint_monitors (
            id text PRIMARY KEY, name text NOT NULL, model text NOT NULL,
            credential_ref text NOT NULL, alerts_enabled boolean NOT NULL DEFAULT false, enabled boolean NOT NULL DEFAULT true,
            next_check_at timestamptz NOT NULL DEFAULT now(),
            lease_token text, lease_until timestamptz,
            last_outcome text, last_checked_at timestamptz, last_success_at timestamptz,
            error text, status_code integer, consecutive_failures integer NOT NULL DEFAULT 0,
            incident_id text, incident_opened_at timestamptz
        );
        CREATE INDEX ix_endpoint_monitors_due ON endpoint_monitors (next_check_at) WHERE enabled;
        CREATE TABLE endpoint_checks (
            monitor_id text NOT NULL REFERENCES endpoint_monitors(id) ON DELETE CASCADE,
            claim_token text NOT NULL, checked_at timestamptz NOT NULL, outcome text NOT NULL,
            latency_ms integer NOT NULL, error text, status_code integer, request_id text,
            incident_id text, PRIMARY KEY (monitor_id, claim_token)
        );
        CREATE INDEX ix_endpoint_checks_history ON endpoint_checks (monitor_id, checked_at);
        CREATE INDEX ix_endpoint_checks_retention ON endpoint_checks (checked_at);
        ALTER TABLE endpoint_monitors ENABLE ROW LEVEL SECURITY;
        ALTER TABLE endpoint_checks ENABLE ROW LEVEL SECURITY;
    """.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE endpoint_checks")
    op.execute("DROP TABLE endpoint_monitors")
