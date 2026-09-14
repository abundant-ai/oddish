"""The new browser reads preserve organization approval and URL query contracts."""

from contextlib import asynccontextmanager
from uuid import uuid4

import auth
import httpx
import pytest
from api.routers import deliveries
from auth.types import AuthContext, AuthMethod
from fastapi import FastAPI
from models import OrganizationModel, UserRole
from oddish.core.deliveries import create_delivery_core
from oddish.db import TaskModel, TaskVersionModel
from oddish.db.connection import engine
from oddish.schemas import DeliveryCreate
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_page_and_selection_check_approval_on_every_request(monkeypatch):
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:

            @asynccontextmanager
            async def reader():
                async with AsyncSession(
                    bind=conn,
                    expire_on_commit=False,
                    join_transaction_mode="create_savepoint",
                ) as session:
                    yield session

            monkeypatch.setattr(auth, "get_read_session", reader)
            org_id = "page_" + uuid4().hex
            async with reader() as session:
                organization = OrganizationModel(
                    id=org_id, name="Page tests", slug=org_id, clerk_org_id=org_id
                )
                session.add(organization)
                task = TaskModel(
                    name="URL task",
                    task_path="s3://fixture/task",
                    org_id=org_id,
                    user="reader",
                )
                session.add(task)
                await session.flush()
                version = TaskVersionModel(
                    id=f"{task.id}-v1",
                    task_id=task.id,
                    version=1,
                    task_path="s3://fixture/v1",
                )
                session.add(version)
                await session.flush()
                task.current_version_id = version.id
                delivery = await create_delivery_core(
                    session,
                    data=DeliveryCreate(
                        customer="Fixture", name="Page", task_ids=[task.id]
                    ),
                    org_id=org_id,
                    user_id="reader",
                )
                await session.commit()
                app = FastAPI()
                app.include_router(deliveries.router)
                context = AuthContext(
                    method=AuthMethod.CLERK_JWT,
                    org_id=org_id,
                    user_id="reader",
                    user_role=UserRole.ADMIN,
                )
                app.dependency_overrides[auth.get_auth_context] = lambda: context
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client:
                    for endpoint in ("view", "selection"):
                        response = await client.get(
                            f"/deliveries/{delivery.id}/{endpoint}"
                        )
                        assert response.status_code == 403
                    organization.execution_enabled = True
                    await session.commit()
                    response = await client.get(
                        f"/deliveries/{delivery.id}/view",
                        params={
                            "page": 99,
                            "per_page": 10,
                            "owner": "mine",
                            "task": task.name,
                        },
                    )
                    assert response.status_code == 200, response.text
                    page = response.json()
                    assert page["page"] == 1 and page["per_page"] == 10
                    assert page["focus_task_id"] == task.id
                    assert page["focus_outside_filters"]
                    assert page["tasks"][0]["version_id"] == version.id
                    response = await client.get(f"/deliveries/{delivery.id}/selection")
                    assert response.status_code == 200, response.text
                    assert response.json()[0]["version_id"] == version.id
                    assert (
                        await client.get(
                            f"/deliveries/{delivery.id}/view?per_page=1000"
                        )
                    ).status_code == 422
                    # Reuse the exact same cached identity after revocation.
                    organization.execution_enabled = False
                    await session.commit()
                    for endpoint in ("view", "selection"):
                        assert (
                            await client.get(f"/deliveries/{delivery.id}/{endpoint}")
                        ).status_code == 403
        finally:
            await transaction.rollback()
