"""Exercise uncached dashboard author resolution against real PostgreSQL."""

import os
import uuid

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select

import dashboard_attribution
from api.routers import dashboard
from auth import AuthContext, AuthMethod, get_auth_context
from models import APIKeyScope, OrganizationModel, UserModel, UserRole
from oddish.core.dashboard import EXPERIMENTS_UNATTRIBUTED_OWNER
from oddish.db import ExperimentModel, TaskModel, get_read_session, get_session
from oddish.db.models import task_experiments

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("ODDISH_DATABASE_URL"), reason="local PostgreSQL required"
    ),
]


@pytest_asyncio.fixture
async def account():
    prefix = uuid.uuid4().hex[:12]
    org_id, user_id = f"org_{prefix}", f"user_{prefix}"
    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id, name=org_id, slug=org_id, execution_enabled=True
            )
        )
        await session.flush()
        session.add(
            UserModel(
                id=user_id,
                org_id=org_id,
                email=f"{prefix}@example.com",
                github_username=prefix,
                role=UserRole.MEMBER,
                attribution_cache=None,
            )
        )
    context = AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id=org_id,
        user_id=user_id,
        user_role=UserRole.MEMBER,
        scope=APIKeyScope.FULL,
    )
    app = FastAPI()
    app.include_router(dashboard.router)
    # Stub identity verification only: the real org approval and route run.
    app.dependency_overrides[get_auth_context] = lambda: context
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, context, prefix
    finally:
        dashboard_attribution.invalidate_attribution_cache(
            org_id=org_id, user_id=user_id
        )
        async with get_session() as session:
            await session.execute(
                task_experiments.delete().where(
                    task_experiments.c.experiment_id.in_(
                        select(ExperimentModel.id).where(
                            ExperimentModel.org_id == org_id
                        )
                    )
                )
            )
            for model in (TaskModel, ExperimentModel, UserModel, OrganizationModel):
                column = model.id if model is OrganizationModel else model.org_id
                await session.execute(model.__table__.delete().where(column == org_id))


@pytest.mark.parametrize("author_filter", ["mine", "member", "search"])
async def test_cold_author_profile_claims_experiments_before_first_read(
    account, author_filter
):
    client, context, handle = account
    experiment_id = f"experiment_{handle}"
    async with get_session() as session:
        session.add(
            ExperimentModel(
                id=experiment_id,
                name=experiment_id,
                org_id=context.org_id,
                owner_user_id=EXPERIMENTS_UNATTRIBUTED_OWNER,
            )
        )
        session.add(
            TaskModel(
                id=f"task_{handle}",
                name=f"task_{handle}",
                org_id=context.org_id,
                user=handle,
                task_path="fixture",
                tags={"github_username": handle},
            )
        )
        await session.flush()
        await session.execute(
            task_experiments.insert().values(
                task_id=f"task_{handle}", experiment_id=experiment_id
            )
        )

    params = {
        "include_queues": "false",
        "include_tasks": "false",
        "include_usage": "false",
        "experiments_author": "me" if author_filter == "mine" else context.user_id,
    }
    if author_filter == "search":
        params["experiments_author"] = "all"
        params["experiments_author_query"] = handle

    for request_number in range(2):
        response = await client.get("/dashboard", params=params)
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["experiments"]] == [experiment_id]
        # A separate connection proves that ownership and the profile committed,
        # rather than merely being visible in the request's identity map.
        async with get_read_session() as session:
            user = await session.get(UserModel, context.user_id)
            assert user.attribution_cache["github_handles"] == [handle]
            experiment = await session.get(ExperimentModel, experiment_id)
            assert experiment.owner_user_id == context.user_id
        if request_number == 0:
            dashboard_attribution.invalidate_attribution_cache(
                org_id=context.org_id, user_id=context.user_id
            )


async def test_mine_without_tasks_returns_empty_results(account):
    client, context, handle = account
    response = await client.get(
        "/dashboard",
        params={
            "experiments_author": "me",
            "include_queues": "false",
            "include_tasks": "false",
            "include_usage": "false",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["experiments"] == []
    async with get_read_session() as session:
        user = await session.get(UserModel, context.user_id)
        assert user.attribution_cache["github_handles"] == [handle]
