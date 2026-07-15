"""Shared test fixtures for the backend test suite.

The backend tests touch the local Postgres database via ``oddish.db``.
``.env.local`` is expected to be loaded by the caller (e.g. ``set -a &&
source .env.local && set +a && uv run pytest``); we don't try to load it
inside the test process to keep the contract explicit.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
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


@pytest_asyncio.fixture
async def session():
    """Async DB session for tests that talk to Postgres directly.

    Rolls back on teardown so shared-DB tests never need to clean up their own
    rows -- only the uniquified slugs they insert are visible mid-test.
    """
    from oddish.db.connection import async_session_maker

    async with async_session_maker() as s:
        try:
            yield s
        finally:
            await s.rollback()
