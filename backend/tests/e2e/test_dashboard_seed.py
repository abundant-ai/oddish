from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from .conftest import DB_URL, E2E_ENABLED, _purge
from .seed_dashboard import TASK_ID, _seed

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (E2E_ENABLED and DB_URL),
        reason="e2e opt-in: set ODDISH_E2E=1 and ODDISH_DATABASE_URL",
    ),
]


@pytest.mark.parametrize("already_exists", [False, True])
async def test_dashboard_seed_approves_new_and_existing_test_orgs(
    schema, already_exists
):
    from models import OrganizationModel
    from oddish.db import TaskModel, get_session
    from org_access import require_execution_org

    clerk_id = f"org_dashboard_{uuid.uuid4().hex[:8]}"
    org_id = f"internal_{clerk_id}" if already_exists else clerk_id
    try:
        if already_exists:
            async with get_session() as session:
                session.add(
                    OrganizationModel(
                        id=org_id,
                        name=org_id,
                        slug=org_id,
                        clerk_org_id=clerk_id,
                    )
                )
        # Repeated seeding must reuse the same org, including one originally
        # created by Clerk login with a different internal database ID.
        assert await _seed(clerk_id) == TASK_ID
        assert await _seed(clerk_id) == TASK_ID
        await require_execution_org(org_id)
        async with get_session() as session:
            org = (
                await session.scalars(
                    select(OrganizationModel).where(
                        OrganizationModel.clerk_org_id == clerk_id,
                    )
                )
            ).one()
            assert org.id == org_id and org.execution_enabled is True
            assert (await session.get(TaskModel, TASK_ID)).org_id == org_id
    finally:
        await _purge(org_id, TASK_ID, "unused")
