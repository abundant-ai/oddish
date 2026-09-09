"""Real delivery HTTP routes and committed version/check writes in local PostgreSQL.

Each HTTP request gets its own session/savepoint. The enclosing transaction rolls
back all fixtures; no workers or application lifespan are started.
"""

from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import TaskModel, TaskVersionModel, TrialModel, WorkerJobModel
from oddish.db.models import DeliveryManualCheckModel


@pytest_asyncio.fixture
async def delivery_api(monkeypatch):
    import oddish.db.connection as connection
    import oddish.server as server
    import oddish.server.deliveries as routes

    async with connection.engine.connect() as conn:
        transaction = await conn.begin()

        @asynccontextmanager
        async def get_session():
            async with AsyncSession(
                bind=conn,
                expire_on_commit=False,
                join_transaction_mode="create_savepoint",
            ) as session:
                yield session

        monkeypatch.setattr(server, "get_session", get_session)
        monkeypatch.setattr(routes, "get_session", get_session)
        async with get_session() as session:
            task = TaskModel(
                name="refresh-api-task", task_path="s3://test/v7", user="tester"
            )
            session.add(task)
            await session.flush()
            version = TaskVersionModel(
                id=f"{task.id}-v7", task_id=task.id, version=7, task_path="s3://test/v7"
            )
            session.add(version)
            await session.flush()
            task.current_version_id = version.id
            await session.commit()
            task_id, version_id = task.id, version.id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server.api), base_url="http://test"
        ) as client:
            yield client, get_session, task_id, version_id
        await transaction.rollback()


async def read_board(client, delivery_id):
    response = await client.get(f"/deliveries/{delivery_id}")
    assert response.status_code == 200, response.text
    return response.json()


def checks(row):
    return {check["key"]: check["status"] for check in row["checks"]}


async def waive_and_sign(client, delivery_id, row):
    for check in row["checks"]:
        if check["kind"] == "automated" and check["status"] == "fail":
            if check["key"] in {"task_exists", "no_must_fix"}:
                continue
            response = await client.put(
                f"/deliveries/{delivery_id}/checks",
                json={
                    "check_key": f"waive:{check['key']}",
                    "delivery_task_id": row["delivery_task_id"],
                    "expected_version_id": row["version_id"],
                    "checked": True,
                },
            )
            assert response.status_code == 200, response.text
    response = await client.put(
        f"/deliveries/{delivery_id}/checks",
        json={
            "check_key": "signoff",
            "delivery_task_id": row["delivery_task_id"],
            "expected_version_id": row["version_id"],
            "checked": True,
        },
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_delivery_reads_follow_selected_version_without_qa(delivery_api):
    client, sessions, task_id, v7 = delivery_api
    response = await client.post(
        "/deliveries",
        json={"customer": "Acme", "name": "refresh", "task_ids": [task_id]},
    )
    assert response.status_code == 200, response.text
    delivery_id = response.json()["id"]
    initial = await read_board(client, delivery_id)
    row = initial["tasks"][0]
    await waive_and_sign(client, delivery_id, row)
    assert (
        checks((await read_board(client, delivery_id))["tasks"][0])["signoff"] == "pass"
    )

    # A stored version can exist without being selected. Creating it is not QA.
    async with sessions() as session:
        version = TaskVersionModel(
            id=f"{task_id}-v8", task_id=task_id, version=8, task_path="s3://test/v8"
        )
        session.add(version)
        await session.commit()
        v8 = version.id
    same = (await read_board(client, delivery_id))["tasks"][0]
    assert same["version_id"] == v7
    assert checks(same)["signoff"] == "pass"
    history = (await client.get(f"/tasks/{task_id}/qa-history")).json()
    assert len(history["versions"]) == 2
    assert history["current_version_id"] == v7

    response = await client.put(f"/tasks/{task_id}/versions/8/default")
    assert response.status_code == 200, response.text
    changed = (await read_board(client, delivery_id))["tasks"][0]
    assert changed["version_id"] == v8
    assert checks(changed)["signoff"] == "fail"
    assert checks(changed)["pre_trial_passed"] == "fail"
    assert changed["qa"]["status"] == "never"
    assert (await client.get(f"/tasks/{task_id}/qa-history")).json()[
        "current_version_id"
    ] == v8

    # A confirmation opened for v7 cannot acknowledge or sign v8, including
    # unchecking a box. The version guard runs before validation or writes.
    for key in ["signoff", "waive:pre_trial_passed", "ack:old-finding"]:
        for checked in [True, False]:
            response = await client.put(
                f"/deliveries/{delivery_id}/checks",
                json={
                    "check_key": key,
                    "delivery_task_id": row["delivery_task_id"],
                    "expected_version_id": v7,
                    "checked": checked,
                },
            )
            assert response.status_code == 409, response.text
            assert "selected task version changed" in response.json()["detail"]
    async with sessions() as session:
        saved = (await session.scalars(select(DeliveryManualCheckModel))).all()
        assert saved and all(tick.task_version_id == v7 for tick in saved)
        assert await session.scalar(select(func.count()).select_from(TrialModel)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(WorkerJobModel)) == 0
        )

    # Assignment writes, membership removal and re-addition appear on the next
    # read; no analysis event is involved.
    response = await client.post(
        f"/deliveries/{delivery_id}/qa-work/claim",
        json={"version_ids": [v8], "limit": 1},
    )
    assert response.status_code == 200, response.text
    assert (await read_board(client, delivery_id))["tasks"][0]["qa_work"][
        "owner_user_id"
    ] == "local"
    response = await client.delete(f"/deliveries/{delivery_id}/tasks/{task_id}")
    assert response.status_code == 200, response.text
    assert (await read_board(client, delivery_id))["tasks"] == []
    response = await client.post(
        f"/deliveries/{delivery_id}/tasks", json={"task_ids": [task_id]}
    )
    assert response.status_code == 200, response.text
    assert (await read_board(client, delivery_id))["tasks"][0]["version_id"] == v8


@pytest.mark.asyncio
async def test_finalized_http_reads_keep_the_shipped_board(delivery_api):
    client, sessions, task_id, v7 = delivery_api
    response = await client.post(
        "/deliveries",
        json={"customer": "Acme", "name": "shipped", "task_ids": [task_id]},
    )
    delivery_id = response.json()["id"]
    await waive_and_sign(
        client, delivery_id, (await read_board(client, delivery_id))["tasks"][0]
    )
    response = await client.post(f"/deliveries/{delivery_id}/finalize")
    assert response.status_code == 200, response.text
    frozen = await read_board(client, delivery_id)
    assert frozen["frozen"] is True
    async with sessions() as session:
        session.add(
            TaskVersionModel(
                id=f"{task_id}-v8", task_id=task_id, version=8, task_path="s3://test/v8"
            )
        )
        await session.commit()
    response = await client.put(f"/tasks/{task_id}/versions/8/default")
    assert response.status_code == 200, response.text
    assert await read_board(client, delivery_id) == frozen
    assert frozen["tasks"][0]["version_id"] == v7
    response = await client.delete(f"/deliveries/{delivery_id}/tasks/{task_id}")
    assert response.status_code == 409, response.text


@pytest.mark.asyncio
async def test_defect_acknowledgment_and_assignment_refresh_without_qa(delivery_api):
    from oddish.db import VerdictStatus

    client, sessions, task_id, v7 = delivery_api
    async with sessions() as session:
        version = await session.get(TaskVersionModel, v7)
        version.pre_trial_status = VerdictStatus.SUCCESS
        version.pre_trial = {
            "items": [{"id": "finding-a", "tier": "must_fix", "title": "Known defect"}]
        }
        await session.commit()
    response = await client.post(
        "/deliveries",
        json={"customer": "Acme", "name": "defects", "task_ids": [task_id]},
    )
    assert response.status_code == 200, response.text
    delivery_id = response.json()["id"]
    row = (await read_board(client, delivery_id))["tasks"][0]
    assert row["defects"][0]["acknowledged"] is False
    for checked in (True, False):
        response = await client.put(
            f"/deliveries/{delivery_id}/checks",
            json={
                "check_key": "ack:finding-a",
                "delivery_task_id": row["delivery_task_id"],
                "expected_version_id": v7,
                "checked": checked,
            },
        )
        assert response.status_code == 200, response.text
        refreshed = (await read_board(client, delivery_id))["tasks"][0]
        assert refreshed["defects"][0]["acknowledged"] is checked
        assert checks(refreshed)["no_must_fix"] == ("pass" if checked else "fail")
    response = await client.post(
        f"/deliveries/{delivery_id}/qa-work/claim",
        json={"version_ids": [v7], "limit": 1},
    )
    assert response.status_code == 200, response.text
    response = await client.patch(
        f"/deliveries/{delivery_id}/qa-work",
        json={
            "version_id": v7,
            "note": "Review this defect",
            "issue_categories": ["verifier"],
        },
    )
    assert response.status_code == 200, response.text
    assert (await read_board(client, delivery_id))["tasks"][0]["qa_work"][
        "note"
    ] == "Review this defect"
    response = await client.patch(
        f"/deliveries/{delivery_id}/qa-work", json={"version_id": v7, "release": True}
    )
    assert response.status_code == 200, response.text
    assert (await read_board(client, delivery_id))["tasks"][0]["qa_work"][
        "owner_user_id"
    ] is None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(TrialModel)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(WorkerJobModel)) == 0
        )
