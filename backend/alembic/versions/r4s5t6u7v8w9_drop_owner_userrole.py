"""Drop the legacy ``owner`` user role.

Collapses the role model to ``admin`` / ``member``. Any existing ``owner``
rows are promoted to ``admin`` before the enum value is removed so the
column type no longer accepts ``owner``.
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "r4s5t6u7v8w9"
down_revision: Union[str, Sequence[str], None] = "chatlistidx01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Promote any legacy owners to admin so the value is unreferenced.
    op.execute("UPDATE users SET role = 'admin' WHERE role = 'owner'")

    # Postgres cannot drop an enum value in place; rebuild the type without it.
    op.execute("ALTER TYPE userrole RENAME TO userrole_old")
    op.execute("CREATE TYPE userrole AS ENUM ('admin', 'member')")
    op.execute("ALTER TABLE users ALTER COLUMN role DROP DEFAULT")
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE userrole "
        "USING role::text::userrole"
    )
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'member'")
    op.execute("DROP TYPE userrole_old")


def downgrade() -> None:
    # Re-add the owner value. Previously-promoted owners cannot be
    # distinguished from real admins, so they stay admin.
    op.execute("ALTER TYPE userrole RENAME TO userrole_old")
    op.execute("CREATE TYPE userrole AS ENUM ('owner', 'admin', 'member')")
    op.execute("ALTER TABLE users ALTER COLUMN role DROP DEFAULT")
    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE userrole "
        "USING role::text::userrole"
    )
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'member'")
    op.execute("DROP TYPE userrole_old")
