import asyncio

import pytest
from fastapi import HTTPException

from oddish.core.deliveries import (
    create_delivery_core,
    finalize_delivery_core,
    get_delivery_board_core,
)
from oddish.core.qa_work import assign_task_qa_work_core
from oddish.db import generate_id, utcnow
from oddish.schemas import DeliveryCreate
from test_deliveries import ORG, _green_task, _sign_off, _task, _version


async def _batch(session, count):
    tasks = [_task(f"assign-{generate_id()}") for _ in range(count)]
    session.add_all(tasks)
    await session.flush()
    versions = [_version(task, 1) for task in tasks]
    session.add_all(versions)
    await session.flush()
    for task, version in zip(tasks, versions, strict=True):
        task.current_version_id = version.id
    await session.flush()
    return tasks, versions


@pytest.mark.asyncio
async def test_assign_200_tasks_without_delivery_then_show_board_ownership(session):
    tasks, versions = await _batch(session, 200)
    versions[0].qa_work = {"note": "Fix verifier", "issue_categories": ["verifier"]}
    await session.flush()
    ids = [task.id for task in reversed(tasks)]
    result = await assign_task_qa_work_core(
        session, org_id=ORG, task_ids=ids + ids[:1], owner_user_id="alice"
    )
    assert result.assigned_task_ids == ids
    assert result.skipped_task_ids == result.unchanged_task_ids == []
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="assign-test", name=generate_id(), task_ids=ids),
        org_id=ORG,
        user_id="admin",
    )
    session.expunge_all()
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert len(board.tasks) == 200
    assert all(row.qa_work.owner_user_id == "alice" for row in board.tasks)
    first = next(row for row in board.tasks if row.task_id == tasks[0].id)
    assert first.qa_work.note == "Fix verifier"
    assert first.qa_work.issue_categories == ["verifier"]


@pytest.mark.asyncio
async def test_assignment_skips_other_owners_until_replace_is_explicit(session):
    tasks, versions = await _batch(session, 3)
    claimed_at = utcnow().isoformat()
    versions[0].qa_work = {"owner_user_id": "alice", "claimed_at": claimed_at}
    versions[1].qa_work = {"owner_user_id": "bob", "note": "Keep this handoff"}
    await session.flush()
    ids = [task.id for task in tasks]
    result = await assign_task_qa_work_core(
        session, org_id=ORG, task_ids=ids, owner_user_id="alice"
    )
    assert result.unchanged_task_ids == [ids[0]]
    assert result.skipped_task_ids == [ids[1]]
    assert result.assigned_task_ids == [ids[2]]
    assert versions[0].qa_work["claimed_at"] == claimed_at
    assert versions[1].qa_work["owner_user_id"] == "bob"
    result = await assign_task_qa_work_core(
        session, org_id=ORG, task_ids=ids, owner_user_id="alice", replace=True
    )
    assert result.assigned_task_ids == [ids[1]]
    assert result.skipped_task_ids == []
    assert versions[1].qa_work["note"] == "Keep this handoff"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "other_org", "deleted", "no_version"])
async def test_invalid_batch_changes_no_owners(session, invalid):
    tasks, versions = await _batch(session, 2)
    ids = [task.id for task in tasks]
    if invalid == "missing":
        ids[1] = "missing-task"
    elif invalid == "other_org":
        tasks[1].org_id = "other-org"
    elif invalid == "deleted":
        tasks[1].deleted_at = utcnow()
    else:
        tasks[1].current_version_id = None
    await session.flush()
    with pytest.raises(HTTPException) as exc:
        await assign_task_qa_work_core(
            session, org_id=ORG, task_ids=ids, owner_user_id="alice"
        )
    assert exc.value.status_code == (409 if invalid == "no_version" else 404)
    assert all(version.qa_work is None for version in versions)


@pytest.mark.asyncio
async def test_assignment_targets_only_the_current_version(session):
    tasks, versions = await _batch(session, 1)
    versions[0].qa_work = {"owner_user_id": "bob"}
    current = _version(tasks[0], 2)
    session.add(current)
    await session.flush()
    tasks[0].current_version_id = current.id
    await session.flush()
    await assign_task_qa_work_core(
        session, org_id=ORG, task_ids=[tasks[0].id], owner_user_id="alice"
    )
    assert versions[0].qa_work["owner_user_id"] == "bob"
    assert current.qa_work["owner_user_id"] == "alice"


@pytest.mark.asyncio
async def test_assignment_updates_active_board_without_changing_finalized_snapshot(
    session,
):
    task, _, _ = await _green_task(session, f"assign-frozen-{generate_id()}")
    deliveries = [
        await create_delivery_core(
            session,
            data=DeliveryCreate(
                customer="assign-test", name=generate_id(), task_ids=[task.id]
            ),
            org_id=ORG,
            user_id="admin",
        )
        for _ in range(2)
    ]
    await _sign_off(session, deliveries[0].id, task.id)
    await finalize_delivery_core(
        session, delivery_id=deliveries[0].id, org_id=ORG, user_id="admin"
    )
    await assign_task_qa_work_core(
        session, org_id=ORG, task_ids=[task.id], owner_user_id="alice"
    )
    frozen = await get_delivery_board_core(
        session, delivery_id=deliveries[0].id, org_id=ORG
    )
    active = await get_delivery_board_core(
        session, delivery_id=deliveries[1].id, org_id=ORG
    )
    assert frozen.frozen
    assert frozen.tasks[0].qa_work.owner_user_id is None
    assert active.tasks[0].qa_work.owner_user_id == "alice"


@pytest.mark.asyncio
async def test_overlapping_assignments_do_not_steal_ownership(session):
    from oddish.db.connection import async_session_maker

    tasks, _ = await _batch(session, 2)
    ids = [task.id for task in tasks]
    await session.commit()

    async def assign(owner, task_ids):
        async with async_session_maker() as worker:
            result = await assign_task_qa_work_core(
                worker, org_id=ORG, task_ids=task_ids, owner_user_id=owner
            )
            await worker.commit()
            return result

    try:
        results = await asyncio.gather(assign("alice", ids), assign("bob", ids[::-1]))
        assert sorted(len(result.assigned_task_ids) for result in results) == [0, 2]
        assert sorted(len(result.skipped_task_ids) for result in results) == [0, 2]
    finally:
        for task in tasks:
            await session.delete(task)
        await session.commit()
