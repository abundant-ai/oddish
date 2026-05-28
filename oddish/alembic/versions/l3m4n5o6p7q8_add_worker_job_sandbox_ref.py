"""add_worker_job_sandbox_ref

Adds two columns to ``worker_jobs`` so we can find and terminate the
remote sandbox a Harbor trial created when the owning worker dies
before normal cleanup runs.

- sandbox_provider: short stable provider id (``"daytona"``, ``"modal"``,
  ...). Mirrors ``BaseEnvironment.provider_name`` upstream.
- sandbox_external_id: provider-side sandbox id (Daytona workspace id,
  Modal sandbox object id, ...).

Populated from Harbor's ENVIRONMENT_START hook while the worker is still
alive, cleared on terminal completion. The stale-heartbeat reaper reads
both columns and dispatches a teardown via the right SDK.

Revision ID: l3m4n5o6p7q8
Revises: k2l3m4n5o6p7
Create Date: 2026-05-28 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "l3m4n5o6p7q8"
down_revision: Union[str, Sequence[str], None] = "k2l3m4n5o6p7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS sandbox_provider TEXT")
    op.execute(
        "ALTER TABLE worker_jobs ADD COLUMN IF NOT EXISTS sandbox_external_id TEXT"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE worker_jobs DROP COLUMN IF EXISTS sandbox_external_id")
    op.execute("ALTER TABLE worker_jobs DROP COLUMN IF EXISTS sandbox_provider")
