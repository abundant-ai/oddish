"""Delivery checklist core: board computation, tick reset, finalize."""

import pytest
from fastapi import HTTPException

from oddish.core.deliveries import (
    add_delivery_tasks_core,
    create_delivery_core,
    finalize_delivery_core,
    get_delivery_board_core,
    get_task_qa_history_core,
    list_deliveries_core,
    patch_delivery_core,
    set_manual_check_core,
)
from oddish.db import (
    DeliverySnapshotModel,
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    generate_id,
)
from oddish.schemas import (
    DeliveryCheckConfig,
    DeliveryCreate,
    DeliveryPatch,
    DeliveryTasksAdd,
    ManualCheckDefinition,
    ManualCheckSet,
)

ORG = "org-deliv"


def _task(name: str) -> TaskModel:
    return TaskModel(
        name=name, org_id=ORG, user="tester", task_path=f"s3://tasks/{name}"
    )


def _version(task: TaskModel, n: int, **kw) -> TaskVersionModel:
    return TaskVersionModel(
        id=f"{task.id}-v{n}",
        task_id=task.id,
        version=n,
        task_path=f"s3://t/{task.id}/v{n}",
        **kw,
    )


def _trial(
    task: TaskModel,
    experiment: ExperimentModel,
    version_id: str,
    *,
    agent: str = "codex",
    kind: str = "agent",
    status: TrialStatus = TrialStatus.SUCCESS,
    analysis: dict | None = None,
) -> TrialModel:
    trial_id = generate_id()
    return TrialModel(
        id=trial_id,
        name=trial_id,
        task_id=task.id,
        task_version_id=version_id,
        experiment_id=experiment.id,
        org_id=ORG,
        agent=agent,
        provider="openai",
        queue_key="openai/gpt-5.5",
        model="gpt-5.5",
        kind=kind,
        status=status,
        analysis=analysis,
        finished_at=None,
    )


async def _green_task(session, name: str):
    """A task whose current version passes every default automated check."""
    experiment = ExperimentModel(name=f"exp-{name}", org_id=ORG)
    task = _task(name)
    session.add_all([experiment, task])
    await session.flush()
    version = _version(
        task,
        1,
        pre_trial_status=VerdictStatus.SUCCESS,
        pre_trial={"items": []},
    )
    session.add(version)
    await session.flush()
    task.current_version_id = version.id
    for i in range(5):
        session.add(
            _trial(task, experiment, version.id, agent=f"agent-{i % 3}")
        )
    session.add(_trial(task, experiment, version.id, kind="qa"))
    task.verdict = {"is_good": True, "verdict": "accept"}
    task.verdict_status = VerdictStatus.SUCCESS
    await session.flush()
    return task, version, experiment


def _checks(board, task_id):
    row = next(r for r in board.tasks if r.task_id == task_id)
    return {c.key: c for c in row.checks}


@pytest.mark.asyncio
async def test_green_task_board_is_ready(session):
    task, _, _ = await _green_task(session, "deliv-green")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-1", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    checks = _checks(board, task.id)
    assert all(c.status == "pass" for c in checks.values()), {
        k: (c.status, c.detail) for k, c in checks.items()
    }
    assert board.ready and board.ready_task_count == 1


@pytest.mark.asyncio
async def test_version_bump_resets_board(session):
    task, _, _ = await _green_task(session, "deliv-bump")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-2", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    v2 = _version(task, 2)
    session.add(v2)
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()

    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    checks = _checks(board, task.id)
    assert checks["pre_trial_passed"].status == "fail"
    assert checks["min_rollouts"].status == "fail"
    assert checks["verdict_ok"].status == "fail"
    assert "older version" in checks["verdict_ok"].detail
    assert not board.ready


@pytest.mark.asyncio
async def test_must_fix_defects_block(session):
    task, version, experiment = await _green_task(session, "deliv-mustfix")
    version.pre_trial = {
        "items": [
            {"tier": "must_fix", "title": "leak"},
            {"tier": "should_fix", "title": "nit"},
        ]
    }
    session.add(
        _trial(
            task,
            experiment,
            version.id,
            analysis={"action_items": [{"tier": "must_fix", "title": "cheat"}]},
        )
    )
    # Malformed analyses (object / scalar action_items) must not crash the
    # board query or count as defects.
    session.add(
        _trial(task, experiment, version.id, analysis={"action_items": {"o": 1}})
    )
    session.add(
        _trial(task, experiment, version.id, analysis={"action_items": "nope"})
    )
    await session.flush()
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-3", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    check = _checks(board, task.id)["no_must_fix"]
    assert check.status == "fail"
    assert "2 must-fix open" in check.detail


@pytest.mark.asyncio
async def test_manual_tick_and_version_reset(session):
    task, _, _ = await _green_task(session, "deliv-manual")
    config = DeliveryCheckConfig(
        manual=[
            ManualCheckDefinition(key="proofread", label="Proofread", scope="task"),
            ManualCheckDefinition(
                key="scope_ok", label="Scope confirmed", scope="delivery"
            ),
        ]
    )
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-4", check_config=config, task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert not board.ready
    member_id = board.tasks[0].delivery_task_id

    await set_manual_check_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=ManualCheckSet(
            check_key="proofread", delivery_task_id=member_id, checked=True
        ),
        user_id="u2",
    )
    await set_manual_check_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=ManualCheckSet(check_key="scope_ok", checked=True),
        user_id="u2",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.ready
    assert _checks(board, task.id)["proofread"].checked_by_user_id == "u2"

    # A new default version stales the task-scoped tick, not the
    # delivery-scoped one.
    v2 = _version(task, 2)
    session.add(v2)
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert _checks(board, task.id)["proofread"].status == "fail"
    assert "older version" in _checks(board, task.id)["proofread"].detail
    assert board.delivery_checks[0].status == "pass"


@pytest.mark.asyncio
async def test_finalize_gates_pins_and_freezes(session):
    task, version, _ = await _green_task(session, "deliv-final")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-5"),
        org_id=ORG,
        user_id="u1",
    )
    with pytest.raises(HTTPException) as err:
        await finalize_delivery_core(
            session, delivery_id=delivery.id, org_id=ORG, user_id="u1"
        )
    assert err.value.status_code == 409  # empty delivery is never ready

    await add_delivery_tasks_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=DeliveryTasksAdd(task_ids=[task.id]),
    )
    board = await finalize_delivery_core(
        session, delivery_id=delivery.id, org_id=ORG, user_id="u1"
    )
    assert board.frozen and delivery.status == "finalized"

    # Versions are pinned and a snapshot row exists with the scope.
    from sqlalchemy import select

    snapshot = await session.scalar(
        select(DeliverySnapshotModel).where(
            DeliverySnapshotModel.delivery_id == delivery.id
        )
    )
    assert snapshot is not None
    assert snapshot.scope == [
        {"task_id": task.id, "task_version_id": version.id}
    ]
    assert snapshot.snapshot["public"]["tasks"][0]["internal_note"] is None

    # Finalized deliveries are read-only, and the board serves the snapshot.
    with pytest.raises(HTTPException) as err:
        await add_delivery_tasks_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=DeliveryTasksAdd(task_ids=[task.id]),
        )
    assert err.value.status_code == 409

    v2 = _version(task, 2)
    session.add(v2)
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.frozen and board.ready  # v2 does not disturb the record


@pytest.mark.asyncio
async def test_org_scoping_and_unknown_check_key(session):
    task, _, _ = await _green_task(session, "deliv-scope")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-6", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    with pytest.raises(HTTPException) as err:
        await get_delivery_board_core(
            session, delivery_id=delivery.id, org_id="other-org"
        )
    assert err.value.status_code == 404

    with pytest.raises(HTTPException) as err:
        await patch_delivery_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=DeliveryPatch(
                check_config=DeliveryCheckConfig(automated={"nope": {}})
            ),
        )
    assert err.value.status_code == 422

    listed = await list_deliveries_core(session, org_id=ORG)
    ours = next(d for d in listed if d.id == delivery.id)
    assert ours.task_count == 1
    assert delivery.id not in {
        d.id for d in await list_deliveries_core(session, org_id="other-org")
    }


@pytest.mark.asyncio
async def test_qa_history(session):
    task, v1, experiment = await _green_task(session, "deliv-history")
    v2 = _version(
        task,
        2,
        pre_trial_status=VerdictStatus.SUCCESS,
        pre_trial={"items": [{"tier": "must_fix", "title": "x"}]},
        message="fix the verifier",
    )
    session.add(v2)
    await session.flush()
    task.current_version_id = v2.id
    session.add(_trial(task, experiment, v2.id, kind="audit"))
    await session.flush()

    history = await get_task_qa_history_core(session, task_id=task.id, org_id=ORG)
    assert [v.version for v in history.versions] == [2, 1]
    latest, first = history.versions
    assert latest.is_current and latest.pre_trial_must_fix == 1
    assert latest.message == "fix the verifier"
    assert [run.kind for run in latest.qa_runs] == ["audit"]
    assert first.rollout_count == 5 and first.rollout_agents == 3
    assert [run.kind for run in first.qa_runs] == ["qa"]
    assert history.verdict == {"is_good": True, "verdict": "accept"}

    with pytest.raises(HTTPException):
        await get_task_qa_history_core(
            session, task_id=task.id, org_id="other-org"
        )


@pytest.mark.asyncio
async def test_deleted_task_blocks_readiness(session):
    task, _, _ = await _green_task(session, "deliv-deleted")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-8", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.ready

    # Soft-deleting a member task must surface as a failing row, not
    # silently shrink the board.
    from oddish.db import utcnow

    task.deleted_at = utcnow()
    await session.flush()
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.task_count == 1 and not board.ready
    row = board.tasks[0]
    assert row.checks[0].key == "task_exists"
    assert "deleted" in row.checks[0].detail

    with pytest.raises(HTTPException) as err:
        await finalize_delivery_core(
            session, delivery_id=delivery.id, org_id=ORG, user_id="u1"
        )
    assert err.value.status_code == 409

    # The remediation path still works: the dead member can be removed.
    from oddish.core.deliveries import remove_delivery_task_core

    await remove_delivery_task_core(
        session, delivery_id=delivery.id, org_id=ORG, task_id=task.id
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.task_count == 0


@pytest.mark.asyncio
async def test_sort_order_advances_from_zero(session):
    a, _, _ = await _green_task(session, "deliv-order-a")
    b, _, _ = await _green_task(session, "deliv-order-b")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-9", task_ids=[a.id]),
        org_id=ORG,
        user_id="u1",
    )
    await add_delivery_tasks_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=DeliveryTasksAdd(task_ids=[b.id]),
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert [(r.task_id, r.sort_order) for r in board.tasks] == [
        (a.id, 0),
        (b.id, 1),
    ]


@pytest.mark.asyncio
async def test_finalized_delivery_cannot_be_deleted(session):
    task, _, _ = await _green_task(session, "deliv-nodelete")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-10", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    await finalize_delivery_core(
        session, delivery_id=delivery.id, org_id=ORG, user_id="u1"
    )
    from oddish.core.deliveries import delete_delivery_core

    with pytest.raises(HTTPException) as err:
        await delete_delivery_core(session, delivery_id=delivery.id, org_id=ORG)
    assert err.value.status_code == 409


@pytest.mark.asyncio
async def test_qa_on_other_version_does_not_stale_verdict(session):
    from datetime import datetime, timezone

    task, v1, experiment = await _green_task(session, "deliv-qa-order")
    # Timestamp the current-version QA run, then add a *later* successful QA
    # on an older version. The current version stays covered: freshness is
    # membership, not global recency.
    qa_current = _trial(task, experiment, v1.id, kind="qa")
    qa_current.finished_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    v0 = _version(task, 2)  # a non-default sibling version
    session.add_all([qa_current, v0])
    await session.flush()
    qa_other = _trial(task, experiment, v0.id, kind="qa")
    qa_other.finished_at = datetime(2026, 8, 15, tzinfo=timezone.utc)
    session.add(qa_other)
    await session.flush()

    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-11", task_ids=[task.id]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert _checks(board, task.id)["verdict_ok"].status == "pass"


@pytest.mark.asyncio
async def test_add_tasks_by_name(session):
    task, _, _ = await _green_task(session, "deliv-by-name")
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(name="batch-12", task_ids=["deliv-by-name"]),
        org_id=ORG,
        user_id="u1",
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.tasks[0].task_id == task.id

    # Names in another org must not resolve, and unknown refs still 404.
    with pytest.raises(HTTPException) as err:
        await add_delivery_tasks_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            data=DeliveryTasksAdd(task_ids=["no-such-task"]),
        )
    assert err.value.status_code == 404

    # Removal accepts a name too.
    from oddish.core.deliveries import remove_delivery_task_core

    await remove_delivery_task_core(
        session, delivery_id=delivery.id, org_id=ORG, task_id="deliv-by-name"
    )
    board = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    assert board.task_count == 0
