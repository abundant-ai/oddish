"""null stale /data/harbor harbor_result_path values

Commit d2154f5 ("removed modal volume stuff") stopped mounting the
shared ``/data`` Modal Volume and switched ``harbor_jobs_dir`` from
``/data/harbor`` to each container's ephemeral ``/tmp/harbor-jobs``.
Any ``trials.harbor_result_path`` written before that commit still
references ``/data/harbor/...`` even though that path can never resolve
on the new containers.  The column is a legacy breadcrumb anyway — the
trial's artifacts are in S3 (see
``StorageClient.upload_trial_results``) and the local dir is deleted
immediately after upload by ``_cleanup_uploaded_job_dir`` — so these
rows are pure noise.

Beyond the noise, leaving them set tripped the path-containment guard
in ``oddish.core.trial_io._resolve_local_job_dir``: trajectory / result
reads for trials without an S3 trajectory would fall through to the
local-fallback branch, hit a path outside the current
``harbor_jobs_dir``, and surface as a confusing ``403`` to the
frontend.  The guard has been softened to return ``None`` for that
case, but nulling the stale rows removes the dead walk entirely and
makes ``harbor_result_path`` stop lying about where artifacts live.

This migration is idempotent: running it repeatedly is a no-op once
the stale rows are nulled.

Revision ID: d5e6f7a8b9c0
Revises: c4b5a6d7e8f9
Create Date: 2026-04-23 16:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, Sequence[str], None] = "c4b5a6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE trials
        SET harbor_result_path = NULL
        WHERE harbor_result_path LIKE '/data/harbor/%'
        """
    )


def downgrade() -> None:
    # Irreversible: the original per-row values are not recoverable, and
    # they pointed at a volume that no longer exists, so there's nothing
    # useful to restore.
    pass
