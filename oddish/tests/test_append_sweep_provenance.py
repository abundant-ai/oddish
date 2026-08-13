from __future__ import annotations

import json

import pytest

from oddish.core.endpoints import create_task_sweep_core, get_task_status_core
from oddish.db import TaskModel, TaskVersionModel, get_session
from oddish.queue import create_task
from oddish.schemas import (
    AgentModelPair,
    TaskSubmission,
    TaskSweepSubmission,
    TrialSpec,
)

pytestmark = pytest.mark.asyncio


def _github_meta(*, sha: str, pr_number: int) -> str:
    return json.dumps(
        {
            "pr_repo": "abundant-ai/sre-world",
            "pr_number": pr_number,
            "pr_url": f"https://github.com/abundant-ai/sre-world/pull/{pr_number}",
            "head_sha": sha,
        }
    )


async def _seed_stale_task_on_new_version(task_id: str) -> tuple[str, str]:
    old_meta = _github_meta(sha="old-sha", pr_number=100)
    version_id = f"{task_id}-v2"
    async with get_session() as session:
        task = await create_task(
            session,
            TaskSubmission(
                name=task_id,
                task_path=f"s3://test-bucket/{task_id}/v1",
                tags={"github_meta": old_meta, "preserved": "yes"},
                trials=[TrialSpec(agent="nop")],
            ),
            task_id=task_id,
        )
        session.add(
            TaskVersionModel(
                id=version_id,
                task_id=task_id,
                version=2,
                task_path=f"s3://test-bucket/{task_id}/v2",
                task_s3_key=f"tasks/{task_id}/v2/",
                content_hash="new-source-hash",
            )
        )
        await session.flush()
        task.current_version_id = version_id
        task.task_path = f"s3://test-bucket/{task_id}/v2"
        task.task_s3_key = f"tasks/{task_id}/v2/"
    return version_id, old_meta


async def _delete_task(task_id: str) -> None:
    async with get_session() as session:
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )


async def test_append_updates_current_task_github_meta_for_new_version() -> None:
    task_id = "append-provenance-new-meta"
    await _delete_task(task_id)
    try:
        version_id, _old_meta = await _seed_stale_task_on_new_version(task_id)
        new_meta = _github_meta(sha="new-exact-sha", pr_number=200)

        async with get_session() as session:
            _task, trials, is_append, _experiment = await create_task_sweep_core(
                session,
                submission=TaskSweepSubmission(
                    task_id=task_id,
                    append_to_task=True,
                    tags={"github_meta": new_meta},
                    configs=[
                        AgentModelPair(
                            agent="nop", model="new-version-provenance", n_trials=1
                        )
                    ],
                ),
            )
            assert is_append
            assert trials
            assert {trial.task_version_id for trial in trials} == {version_id}

        async with get_session() as session:
            status = await get_task_status_core(
                session, task_id=task_id, include_trials=False
            )
            task = await session.get(TaskModel, task_id)

        assert status.current_version_id == version_id
        assert status.github_meta == {
            "pr_repo": "abundant-ai/sre-world",
            "pr_number": "200",
            "pr_url": "https://github.com/abundant-ai/sre-world/pull/200",
            "head_sha": "new-exact-sha",
        }
        assert task is not None
        assert task.tags["preserved"] == "yes"
    finally:
        await _delete_task(task_id)


async def test_append_without_tags_preserves_existing_github_meta() -> None:
    task_id = "append-provenance-omitted-meta"
    await _delete_task(task_id)
    try:
        version_id, old_meta = await _seed_stale_task_on_new_version(task_id)

        async with get_session() as session:
            _task, trials, is_append, _experiment = await create_task_sweep_core(
                session,
                submission=TaskSweepSubmission(
                    task_id=task_id,
                    append_to_task=True,
                    configs=[
                        AgentModelPair(
                            agent="nop", model="omitted-version-provenance", n_trials=1
                        )
                    ],
                ),
            )
            assert is_append
            assert trials
            assert {trial.task_version_id for trial in trials} == {version_id}

        async with get_session() as session:
            status = await get_task_status_core(
                session, task_id=task_id, include_trials=False
            )
            task = await session.get(TaskModel, task_id)

        assert status.current_version_id == version_id
        assert status.github_meta == {
            "pr_repo": "abundant-ai/sre-world",
            "pr_number": "100",
            "pr_url": "https://github.com/abundant-ai/sre-world/pull/100",
            "head_sha": "old-sha",
        }
        assert task is not None
        assert task.tags["github_meta"] == old_meta
        assert task.tags["preserved"] == "yes"
    finally:
        await _delete_task(task_id)
