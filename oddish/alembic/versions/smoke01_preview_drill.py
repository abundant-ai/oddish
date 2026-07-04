"""TEMPORARY preview-rehearsal drill -- reverted before merge, never runs on prod.

Proves a branch-only migration executes on the PR's preview database and
nowhere else: the preview rebuild applies it via ``alembic upgrade head``
(look for ``Running upgrade ixdrift01_add_model_indexes ->
smoke01_preview_drill`` in the Prepare preview database log), while prod
migrations only ever run from pushes to main. The INSERT additionally proves
data-writing ``op.execute`` steps run against the seeded preview data -- the
class the old create_all + stamp flow never executed anywhere before prod.
"""

from alembic import op

revision = "smoke01_preview_drill"
down_revision = "ixdrift01_add_model_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS preview_rehearsal_smoke (
            id BIGSERIAL PRIMARY KEY,
            note TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "INSERT INTO preview_rehearsal_smoke (note) "
        "VALUES ('branch-only migration executed by the preview rehearsal')"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS preview_rehearsal_smoke")
