"""Shared test fixtures for the backend test suite.

The backend tests touch the local Postgres database via ``oddish.db``.
``.env.local`` is expected to be loaded by the caller (e.g. ``set -a &&
source .env.local && set +a && uv run pytest``); we don't try to load it
inside the test process to keep the contract explicit.
"""

from __future__ import annotations

import pytest
from sqlalchemy import ForeignKeyConstraint

# The OSS schema deliberately omits model-level FKs on ``api_keys``
# (oddish/db/models.py: "In Cloud: FK constraints are added via migration"), so
# ``api_keys.org_id`` / ``api_keys.created_by_user_id`` carry no ForeignKey in
# the SQLAlchemy metadata. Mapper configuration of ``OrganizationModel.api_keys``
# and ``UserModel.api_keys`` then cannot determine a join condition, and every
# backend DB test errors with NoForeignKeysError before it runs. Re-add exactly
# the constraints the cloud migration installs to the in-memory table metadata
# (test process only; no product code or live schema is touched) so those
# relationships resolve.
import models  # noqa: E402,F401  registers org/user/api_key tables on shared Base
from models import APIKeyModel  # noqa: E402

_api_keys_table = APIKeyModel.__table__
if not _api_keys_table.c.org_id.foreign_keys:
    _api_keys_table.append_constraint(
        ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_api_keys_org_id",
            ondelete="CASCADE",
        )
    )
if not _api_keys_table.c.created_by_user_id.foreign_keys:
    _api_keys_table.append_constraint(
        ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name="fk_api_keys_created_by_user_id",
            ondelete="SET NULL",
        )
    )


# pytest-asyncio strict mode is fine: tests opt in with @pytest.mark.asyncio.
# We declare the loop scope here so async fixtures share the loop with the
# tests that consume them.
pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def _bootstrap_db_schema():
    """Create the full table set once per session when a test DB is configured.

    Several DB-backed suites (admin costs, analyzers router, github-id
    backfill) assume the tables already exist rather than creating them.
    Historically they leaned on an earlier-collected suite's drop-and-create
    fixture to have run first; make that bootstrap explicit and ordering-proof.
    Non-destructive (``checkfirst``): suites that want a pristine schema still
    reset it themselves.
    """
    import asyncio
    import os

    url = os.environ.get("ODDISH_DATABASE_URL")
    if url:
        from oddish.db.models import Base
        from sqlalchemy.ext.asyncio import create_async_engine

        async def _create_all() -> None:
            engine = create_async_engine(url)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
            finally:
                await engine.dispose()

        asyncio.run(_create_all())
    yield
