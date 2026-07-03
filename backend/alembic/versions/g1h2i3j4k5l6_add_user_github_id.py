"""add users.github_id"""

from typing import Sequence, Union

from alembic import op

revision: str = "g1h2i3j4k5l6"
down_revision: Union[str, Sequence[str], None] = "t6u7v8w9x0y1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS github_id TEXT")
    # The unique constraint below already backs (org_id, github_id) lookups with
    # its own index, so a separate non-unique index is pure write overhead. The
    # DROP is a no-op on fresh databases; it only fires where this statement runs
    # against a DB that got the index some other way (e.g. a create_all-built
    # preview from the old model). Note Alembic does NOT re-run this revision on
    # DBs that already applied its earlier version — those previews keep the
    # redundant index until rebuilt, which per-PR preview DBs routinely are.
    op.execute("DROP INDEX IF EXISTS idx_users_org_github_id")
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'uq_users_org_github_id'
            ) THEN
                ALTER TABLE users
                ADD CONSTRAINT uq_users_org_github_id UNIQUE (org_id, github_id);
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP CONSTRAINT IF EXISTS uq_users_org_github_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS github_id")
