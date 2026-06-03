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

    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    from oddish.db.models import Base

    bind = op.get_bind()
    Base.metadata.drop_all(bind)
