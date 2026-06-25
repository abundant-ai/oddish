"""drop api_keys cross-stack foreign keys

``api_keys`` lives in the oddish stack but its FKs target ``organizations``
and ``users``, which are created by the backend migration chain that runs
*after* oddish. A fresh ``oddish`` upgrade therefore cannot resolve the
constraint (``Base.metadata.create_all`` in ``000_initial_schema`` raises
``NoReferencedTableError``), so the oddish stack can't bootstrap standalone.

Drop the two cross-stack FKs and keep the columns (``org_id`` stays indexed
via ``idx_api_keys_org_id``). Integrity is enforced in backend auth, which
resolves keys by joining ``api_keys`` -> ``organizations``. Prod, already
past ``000_initial_schema``, converges here; fresh DBs never create the FK.

Revision ID: apk01dropfk
Revises: hv2claimidx01
Create Date: 2026-06-24
"""

from typing import Sequence, Union

from alembic import op


revision: str = "apk01dropfk"
down_revision: Union[str, Sequence[str], None] = "hv2claimidx01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_org_id_fkey")
    op.execute(
        "ALTER TABLE api_keys DROP CONSTRAINT IF EXISTS api_keys_created_by_user_id_fkey"
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE api_keys
        ADD CONSTRAINT api_keys_org_id_fkey
        FOREIGN KEY (org_id) REFERENCES organizations (id) ON DELETE CASCADE
        """
    )
    op.execute(
        """
        ALTER TABLE api_keys
        ADD CONSTRAINT api_keys_created_by_user_id_fkey
        FOREIGN KEY (created_by_user_id) REFERENCES users (id) ON DELETE SET NULL
        """
    )
