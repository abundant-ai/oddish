"""Delivery freshness and shared ownership against real PostgreSQL rows."""

import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException
from oddish.core.analysis_payload import audit_fingerprint, qa_trial_evidence
from oddish.core.deliveries import (
    _customer_safe_board,
    claim_delivery_qa_core,
    create_delivery_core,
    get_delivery_board_core,
    patch_delivery_qa_work_core,
)
from oddish.db import TrialModel, TrialStatus, generate_id, utcnow
from oddish.schemas import DeliveryCreate, QAWorkClaim, QAWorkPatch
from sqlalchemy import select
from test_deliveries import ORG, _green_task, _trial, _version


async def _reviewed_delivery(session):
    task, version, experiment = await _green_task(session, f"qa-board-{generate_id()}")
    runs = list(
        (
            await session.scalars(
                select(TrialModel).where(TrialModel.task_id == task.id)
            )
        ).all()
    )
    qa = next(row for row in runs if row.kind == "qa")
    sources = [row for row in runs if row.kind == "agent"]
    for source in sources:
        source.finished_at = utcnow() - timedelta(hours=3)
    qa.created_at = utcnow() - timedelta(hours=2)
    qa.finished_at = utcnow() - timedelta(hours=1)
    qa.harbor_config = {
        "analysis_payload": {
            "trial_ids": [row.id for row in sources],
            "trial_evidence": [qa_trial_evidence(row) for row in sources],
            "baseline_evidence": [],
            "audit_fingerprint": audit_fingerprint(version),
        }
    }
    delivery = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="qa-test", name=generate_id(), task_ids=[task.id]),
        org_id=ORG,
        user_id="alice",
    )
    await session.flush()
    return delivery, task, version, experiment, qa, sources


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [True, False])
async def test_completed_qa_covers_current_evidence(session, accepted):
    delivery, task, _, _, qa, _ = await _reviewed_delivery(session)
    task.verdict = {
        "is_good": accepted,
        "primary_issue": "Verifier accepts wrong answers",
    }
    await session.flush()
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert board.tasks[0].qa.status == ("accepted" if accepted else "needs_fixes")
    assert board.tasks[0].qa.trial_id == qa.id
    assert board.tasks[0].qa.finished_at == qa.finished_at
    assert board.qa_as_of is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "new_trial",
        "same_trial_rerun",
        "reward",
        "new_version",
        "audit",
        "legacy",
        "no_verdict",
        "error",
        "queued",
        "running",
    ],
)
async def test_qa_status_tracks_replacement_and_evidence_changes(session, change):
    delivery, task, version, experiment, qa, sources = await _reviewed_delivery(session)
    expected = "outdated"
    if change == "new_trial":
        session.add(_trial(task, experiment, version.id))
    elif change == "same_trial_rerun":
        sources[0].finished_at = utcnow()
    elif change == "reward":
        sources[0].reward = 0.25
    elif change == "new_version":
        newer = _version(task, 2)
        session.add(newer)
        await session.flush()
        task.current_version_id = newer.id
    elif change == "audit":
        version.pre_trial_finished_at = utcnow()
    elif change == "legacy":
        qa.harbor_config = {}
    elif change == "no_verdict":
        task.verdict = None
        expected = "error"
    else:
        expected = change
        replacement = _trial(
            task,
            experiment,
            version.id,
            kind="qa",
            status={
                "error": TrialStatus.FAILED,
                "queued": TrialStatus.QUEUED,
                "running": TrialStatus.RUNNING,
            }[change],
        )
        replacement.created_at = utcnow()
        session.add(replacement)
    await session.flush()
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert board.tasks[0].qa.status == expected


@pytest.mark.asyncio
async def test_admission_exclusions_do_not_make_qa_outdated(session):
    delivery, task, version, experiment, _, _ = await _reviewed_delivery(session)
    skipped = _trial(task, experiment, version.id, status=TrialStatus.SKIPPED)
    imported = _trial(task, experiment, version.id)
    imported.imported_at = utcnow()
    superseded = _trial(task, experiment, version.id)
    superseded.superseded_by_trial_id = skipped.id
    session.add_all([skipped, imported, superseded])
    await session.flush()
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert board.tasks[0].qa.status == "accepted"


@pytest.mark.asyncio
async def test_legacy_missing_evidence_is_not_current_even_when_sources_are_gone(
    session,
):
    delivery, _, _, _, qa, sources = await _reviewed_delivery(session)
    qa.harbor_config = {"analysis_payload": {"trial_ids": [sources[0].id]}}
    for source in sources:
        source.deleted_at = utcnow()
    await session.flush()
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert board.tasks[0].qa.status == "outdated"


@pytest.mark.asyncio
async def test_customer_snapshot_omits_qa_coordination(session):
    delivery, _, version, _, _, _ = await _reviewed_delivery(session)
    version.qa_work = {"owner_user_id": "alice", "note": "Internal debugging note"}
    await session.flush()
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    board.qa_viewer_user_id = "alice"
    public = _customer_safe_board(board)
    assert "qa_viewer_user_id" not in public
    assert not {"qa", "qa_work", "qa_owner_name"}.intersection(public["tasks"][0])
    assert board.tasks[0].qa_work.note == "Internal debugging note"


@pytest.mark.asyncio
async def test_claim_limit_preserves_candidate_order(session):
    delivery, first_task, first, _, _, _ = await _reviewed_delivery(session)
    second_task, second, _ = await _green_task(session, f"qa-order-{generate_id()}")
    from oddish.core.deliveries import add_delivery_tasks_core
    from oddish.schemas import DeliveryTasksAdd

    await add_delivery_tasks_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        data=DeliveryTasksAdd(task_ids=[second_task.id]),
    )
    claimed = await claim_delivery_qa_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        user_id="alice",
        data=QAWorkClaim(version_ids=[second.id, first.id], limit=1),
    )
    assert claimed == [second.id]
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert (
        next(
            row for row in board.tasks if row.task_id == first_task.id
        ).qa_work.owner_user_id
        is None
    )


@pytest.mark.asyncio
async def test_claims_and_notes_are_shared_across_deliveries(session):
    delivery, task, version, _, _, _ = await _reviewed_delivery(session)
    other = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="qa-test", name=generate_id(), task_ids=[task.id]),
        org_id=ORG,
        user_id="bob",
    )
    request = QAWorkClaim(version_ids=[version.id])
    assert await claim_delivery_qa_core(
        session, delivery_id=delivery.id, org_id=ORG, user_id="alice", data=request
    ) == [version.id]
    assert (
        await claim_delivery_qa_core(
            session, delivery_id=other.id, org_id=ORG, user_id="bob", data=request
        )
        == []
    )
    with pytest.raises(HTTPException) as exc:
        await patch_delivery_qa_work_core(
            session,
            delivery_id=other.id,
            org_id=ORG,
            user_id="bob",
            is_admin=False,
            data=QAWorkPatch(version_id=version.id, release=True),
        )
    assert exc.value.status_code == 403
    await patch_delivery_qa_work_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        user_id="alice",
        is_admin=False,
        data=QAWorkPatch(
            version_id=version.id,
            issue_categories=["verifier", "evidence"],
            note="Fix reward check",
        ),
    )
    board = await get_delivery_board_core(session, delivery_id=other.id, org_id=ORG)
    assert board.tasks[0].qa_work.note == "Fix reward check"
    assert board.tasks[0].qa_work.owner_user_id == "alice"
    await patch_delivery_qa_work_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        user_id="admin",
        is_admin=True,
        data=QAWorkPatch(version_id=version.id, release=True),
    )
    assert await claim_delivery_qa_core(
        session, delivery_id=other.id, org_id=ORG, user_id="bob", data=request
    ) == [version.id]


@pytest.mark.asyncio
async def test_claim_rejects_wrong_org_frozen_and_old_version(session):
    delivery, task, version, _, _, _ = await _reviewed_delivery(session)
    request = QAWorkClaim(version_ids=[version.id])
    with pytest.raises(HTTPException) as exc:
        await claim_delivery_qa_core(
            session,
            delivery_id=delivery.id,
            org_id="different-org",
            user_id="bob",
            data=request,
        )
    assert exc.value.status_code == 404
    newer = _version(task, 2)
    session.add(newer)
    await session.flush()
    task.current_version_id = newer.id
    await session.flush()
    assert (
        await claim_delivery_qa_core(
            session, delivery_id=delivery.id, org_id=ORG, user_id="alice", data=request
        )
        == []
    )
    with pytest.raises(HTTPException) as exc:
        await patch_delivery_qa_work_core(
            session,
            delivery_id=delivery.id,
            org_id=ORG,
            user_id="alice",
            is_admin=True,
            data=QAWorkPatch(version_id=version.id, note="stale edit"),
        )
    assert exc.value.status_code == 409
    delivery.status = "finalized"
    delivery.finalized_at = utcnow()
    await session.flush()
    with pytest.raises(HTTPException) as exc:
        await claim_delivery_qa_core(
            session, delivery_id=delivery.id, org_id=ORG, user_id="alice", data=request
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_concurrent_claims_through_different_deliveries_have_one_owner(session):
    from oddish.db.connection import async_session_maker

    delivery, task, version, experiment, _, _ = await _reviewed_delivery(session)
    other = await create_delivery_core(
        session,
        data=DeliveryCreate(customer="qa-test", name=generate_id(), task_ids=[task.id]),
        org_id=ORG,
        user_id="bob",
    )
    await session.commit()

    async def claim(delivery_id, user):
        async with async_session_maker() as worker:
            result = await claim_delivery_qa_core(
                worker,
                delivery_id=delivery_id,
                org_id=ORG,
                user_id=user,
                data=QAWorkClaim(version_ids=[version.id]),
            )
            await worker.commit()
            return result

    try:
        results = await asyncio.gather(
            claim(delivery.id, "alice"), claim(other.id, "bob")
        )
        assert sum(len(result) for result in results) == 1
    finally:
        await session.delete(delivery)
        await session.delete(other)
        await session.delete(task)
        await session.delete(experiment)
        await session.commit()
