"""initial_schema

Materializes the full oddish schema from the live SQLAlchemy model
graph so the migration chain self-bootstraps. Production was originally
created via ``init_db()`` (``Base.metadata.create_all``) and every
migration since assumed those tables exist; previews and fresh
self-hosters had no path. This is the missing first step.

Prod stays untouched: prod's ``alembic_version_oddish`` row already
points past this migration, so ``alembic upgrade head`` against prod is
a no-op.

Revision ID: 000_initial_schema
Revises:
Create Date: 2026-04-25
"""

from typing import Sequence, Union

from alembic import op


revision: str = "000_initial_schema"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from oddish.db.models import Base

    bind = op.get_bind()
    Base.metadata.create_all(bind)

    # ``QueueSlotModel`` inherits ``id``/``created_at``/``updated_at``/
    # ``deleted_at`` from ``Base`` (a long-standing latent bug — the
    # model has a composite PK of (queue_key, slot) but the inherited
    # PK ``id`` ends up in the table too). Prod was bootstrapped from
    # the dedicated ``r3s4t5u6v7w8`` migration's raw SQL, which only
    # creates the four real columns, so prod has never had the extras.
    # Strip them here so fresh installs match prod.
    op.execute("ALTER TABLE queue_slots DROP COLUMN IF EXISTS id")
    op.execute("ALTER TABLE queue_slots DROP COLUMN IF EXISTS created_at")
    op.execute("ALTER TABLE queue_slots DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE queue_slots DROP COLUMN IF EXISTS deleted_at")


def downgrade() -> None:
    from oddish.db.models import Base

    bind = op.get_bind()
    Base.metadata.drop_all(bind)
