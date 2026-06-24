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
    # Guarded so the chain stays replay-safe. On a schema built from the current
    # model graph (e.g. a Supabase data-less branch or ``Base.metadata.create_all``)
    # the ``userrole`` enum is already ``('admin', 'member')`` and ``a1b2c3d4e5f6``'s
    # ``CREATE TYPE ... IF duplicate skip`` left it that way, so this rebuild would
    # otherwise fail (``UPDATE ... WHERE role = 'owner'`` rejects the unknown enum
    # label). Only run when ``owner`` is still a value of the enum. A true empty-DB
    # replay creates the enum with ``owner`` in ``a1b2c3d4e5f6`` and still rebuilds
    # it here.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_enum e
                JOIN pg_type t ON t.oid = e.enumtypid
                WHERE t.typname = 'userrole' AND e.enumlabel = 'owner'
            ) THEN
                -- Promote any legacy owners to admin so the value is unreferenced.
                UPDATE users SET role = 'admin' WHERE role = 'owner';
                -- Postgres cannot drop an enum value in place; rebuild without it.
                ALTER TYPE userrole RENAME TO userrole_old;
                CREATE TYPE userrole AS ENUM ('admin', 'member');
                ALTER TABLE users ALTER COLUMN role DROP DEFAULT;
                ALTER TABLE users ALTER COLUMN role TYPE userrole
                    USING role::text::userrole;
                ALTER TABLE users ALTER COLUMN role SET DEFAULT 'member';
                DROP TYPE userrole_old;
            END IF;
        END $$;
        """
    )


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
