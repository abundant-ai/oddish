"""HTTP round-trip coverage for the task-registry metadata/provenance
filters on ``GET /tasks/browse`` (the router that actually serves
oddish.app), not just ``browse_tasks_core`` directly.

This branch has repeatedly hit the same defect: a filter value accepted by
the ``Query(...)`` param but never forwarded in the ``browse_tasks_core(...)``
call below it, so the request succeeds (200) and silently returns an
unfiltered page. ``test_source_repo_filter_end_to_end`` below is written so
that dropping either the ``Query`` param or its pass-through kwarg makes the
test fail loudly, rather than merely checking the param is declared.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from auth import AuthContext, AuthMethod, require_auth
from oddish.db import get_session
from oddish.db.models import TaskModel, TaskUploadEventModel, TaskVersionModel


@pytest.fixture
def org_id() -> str:
    return f"org_taskreg_query_{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture
async def client(org_id):
    app = create_app()
    fake_auth = AuthContext(
        method=AuthMethod.CLERK_JWT, org_id=org_id, user_id="user-taskreg-query"
    )
    app.dependency_overrides[require_auth] = lambda: fake_auth
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as async_client:
            yield async_client
    finally:
        app.dependency_overrides.clear()


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_tasks(org_id):
    yield
    async with get_session() as session:
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.org_id == org_id)
        )
        await session.commit()


async def _seed_two_tasks(org_id: str) -> tuple[str, str]:
    """One task with an upload event from harbor-lh, one without. Returns
    (matching_task_id, other_task_id)."""
    async with get_session() as session:
        matching = TaskModel(
            name=f"harbor-lh-task-{org_id}",
            org_id=org_id,
            user="tester",
            task_path="s3://tasks/x",
        )
        other = TaskModel(
            name=f"other-task-{org_id}",
            org_id=org_id,
            user="tester",
            task_path="s3://tasks/y",
        )
        session.add_all([matching, other])
        await session.flush()

        for task in (matching, other):
            version = TaskVersionModel(
                id=f"{task.id}-v1", task_id=task.id, version=1, task_path=task.task_path
            )
            session.add(version)
            await session.flush()
            task.current_version_id = version.id

        session.add(
            TaskUploadEventModel(
                task_id=matching.id,
                task_version_id=matching.current_version_id,
                created_version=True,
                source_repo="abundant-ai/harbor-lh",
            )
        )
        session.add(
            TaskUploadEventModel(
                task_id=other.id,
                task_version_id=other.current_version_id,
                created_version=True,
                source_repo="someone/laptop",
            )
        )
        await session.commit()
    return matching.id, other.id


@pytest.mark.asyncio
async def test_source_repo_filter_end_to_end(client, org_id):
    matching_id, other_id = await _seed_two_tasks(org_id)

    resp = await client.get(
        "/tasks/browse", params={"source_repo": "abundant-ai/harbor-lh"}
    )
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert matching_id in ids
    assert other_id not in ids


@pytest.mark.asyncio
async def test_allow_internet_filter_end_to_end(client, org_id):
    async with get_session() as session:
        online = TaskModel(
            name=f"online-{org_id}", org_id=org_id, user="tester", task_path="s3://t/1"
        )
        offline = TaskModel(
            name=f"offline-{org_id}", org_id=org_id, user="tester", task_path="s3://t/2"
        )
        session.add_all([online, offline])
        await session.flush()
        for task, allow in ((online, True), (offline, False)):
            version = TaskVersionModel(
                id=f"{task.id}-v1",
                task_id=task.id,
                version=1,
                task_path=task.task_path,
                allow_internet=allow,
            )
            session.add(version)
            await session.flush()
            task.current_version_id = version.id
        await session.commit()
        online_id, offline_id = online.id, offline.id

    resp = await client.get("/tasks/browse", params={"allow_internet": "true"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert online_id in ids
    assert offline_id not in ids


@pytest.mark.asyncio
async def test_gpus_cpus_memory_filter_end_to_end(client, org_id):
    async with get_session() as session:
        beefy = TaskModel(
            name=f"beefy-{org_id}", org_id=org_id, user="tester", task_path="s3://t/5"
        )
        tiny = TaskModel(
            name=f"tiny-{org_id}", org_id=org_id, user="tester", task_path="s3://t/6"
        )
        session.add_all([beefy, tiny])
        await session.flush()
        for task, gpus, cpus, memory_mb in (
            (beefy, 4, 16, 65536),
            (tiny, 0, 1, 512),
        ):
            version = TaskVersionModel(
                id=f"{task.id}-v1",
                task_id=task.id,
                version=1,
                task_path=task.task_path,
                gpus=gpus,
                cpus=cpus,
                memory_mb=memory_mb,
            )
            session.add(version)
            await session.flush()
            task.current_version_id = version.id
        await session.commit()
        beefy_id, tiny_id = beefy.id, tiny.id

    resp = await client.get(
        "/tasks/browse", params={"gpus_min": "2", "gpus_max": "8"}
    )
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert beefy_id in ids
    assert tiny_id not in ids

    resp = await client.get("/tasks/browse", params={"cpus_min": "8"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert beefy_id in ids
    assert tiny_id not in ids

    resp = await client.get("/tasks/browse", params={"memory_mb_min": "32768"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert beefy_id in ids
    assert tiny_id not in ids


@pytest.mark.asyncio
async def test_ci_pr_number_and_uploader_is_ci_filter_end_to_end(client, org_id):
    matching_id, other_id = await _seed_two_tasks(org_id)
    async with get_session() as session:
        await session.execute(
            TaskUploadEventModel.__table__.update()
            .where(TaskUploadEventModel.task_id == matching_id)
            .values(ci_pr_number=482, uploader_is_ci=True)
        )
        await session.execute(
            TaskUploadEventModel.__table__.update()
            .where(TaskUploadEventModel.task_id == other_id)
            .values(ci_pr_number=None, uploader_is_ci=False)
        )
        await session.commit()

    resp = await client.get("/tasks/browse", params={"ci_pr_number": "482"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert matching_id in ids
    assert other_id not in ids

    resp = await client.get("/tasks/browse", params={"uploader_is_ci": "true"})
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert matching_id in ids
    assert other_id not in ids


@pytest.mark.asyncio
async def test_expert_hours_filter_end_to_end(client, org_id):
    async with get_session() as session:
        easy = TaskModel(
            name=f"easy-{org_id}",
            org_id=org_id,
            user="tester",
            task_path="s3://t/3",
            expert_time_hours=1.0,
        )
        hard = TaskModel(
            name=f"hard-{org_id}",
            org_id=org_id,
            user="tester",
            task_path="s3://t/4",
            expert_time_hours=40.0,
        )
        session.add_all([easy, hard])
        await session.commit()
        easy_id, hard_id = easy.id, hard.id

    resp = await client.get(
        "/tasks/browse", params={"expert_hours_min": "10", "expert_hours_max": "100"}
    )
    assert resp.status_code == 200, resp.text
    ids = {item["id"] for item in resp.json()["items"]}
    assert hard_id in ids
    assert easy_id not in ids
