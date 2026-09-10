"""Delivery history uses actual observations, independent of browser reads."""

from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from oddish.core.deliveries import (
    _customer_safe_board,
    create_delivery_core,
    finalize_delivery_core,
    get_delivery_board_core,
    set_manual_check_core,
)
from oddish.core.delivery_progress import (
    delivery_progress_history,
    record_delivery_progress,
)
from oddish.db import DeliveryProgressModel, utcnow
from oddish.schemas import DeliveryCreate, ManualCheckSet
from test_deliveries import ORG, _green_task


@pytest.mark.asyncio
async def test_hourly_observations_daily_history_and_read_only_board(session):
    task, version, _ = await _green_task(session, "progress")
    delivery = await create_delivery_core(
        session,
        org_id=ORG,
        user_id="reviewer",
        data=DeliveryCreate(name="progress", customer="customer", task_ids=[task.id]),
    )
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert board.progress_history == []
    assert (
        await session.scalar(select(func.count()).select_from(DeliveryProgressModel))
        == 0
    )
    now = utcnow().replace(hour=12, minute=0, second=0, microsecond=0)
    await record_delivery_progress(session, board, recorded_at=now - timedelta(days=2))
    await record_delivery_progress(session, board, recorded_at=now)
    version.qa_work = {"owner_user_id": "reviewer"}
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    await record_delivery_progress(
        session, board, recorded_at=now + timedelta(minutes=10)
    )
    assert (
        await session.scalar(select(func.count()).select_from(DeliveryProgressModel))
        == 2
    )
    await record_delivery_progress(session, board, recorded_at=now + timedelta(hours=1))
    history = await delivery_progress_history(session, delivery.id)
    assert len(history) == 2  # Missing yesterday was not invented as a zero.
    assert history[0].unassigned == 1
    assert history[1].unassigned == 0
    assert history[1].recorded_at == now + timedelta(hours=1)
    assert history[1].awaiting_signoff == 1
    assert history[1].ready == history[1].blocked == 0
    with pytest.raises(HTTPException) as denied:
        await get_delivery_board_core(
            session, delivery_id=delivery.id, org_id="another-org"
        )
    assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_finalization_records_and_freezes_progress(session):
    task, _, _ = await _green_task(session, "progress-final")
    delivery = await create_delivery_core(
        session,
        org_id=ORG,
        user_id="reviewer",
        data=DeliveryCreate(
            name="progress-final", customer="customer", task_ids=[task.id]
        ),
    )
    current = await get_delivery_board_core(
        session, delivery_id=delivery.id, org_id=ORG
    )
    await set_manual_check_core(
        session,
        delivery_id=delivery.id,
        org_id=ORG,
        user_id="reviewer",
        data=ManualCheckSet(
            check_key="signoff",
            delivery_task_id=current.tasks[0].delivery_task_id,
            checked=True,
            expected_version_id=task.current_version_id,
        ),
    )
    board = await finalize_delivery_core(
        session, delivery_id=delivery.id, org_id=ORG, user_id="reviewer"
    )
    assert board.progress_history[-1].ready == 1
    assert board.progress_history[-1].unassigned == 0
    assert "progress_history" not in _customer_safe_board(board)
    # Later task changes cannot rewrite what the finalized delivery shows.
    task.verdict = {"is_good": False, "verdict": "reject"}
    await session.flush()
    frozen = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    assert frozen.frozen
    assert frozen.progress_history == board.progress_history


@pytest.mark.asyncio
async def test_open_findings_block_even_when_checks_pass(session):
    task, _, _ = await _green_task(session, "progress-defect")
    delivery = await create_delivery_core(
        session,
        org_id=ORG,
        user_id="reviewer",
        data=DeliveryCreate(
            name="progress-defect", customer="customer", task_ids=[task.id]
        ),
    )
    board = await get_delivery_board_core(session, delivery_id=delivery.id, org_id=ORG)
    from oddish.schemas import DeliveryDefect

    board.tasks[0].defects = [
        DeliveryDefect(
            id="retained",
            title="Reported defect",
            source="pre_trial",
            acknowledged=False,
        )
    ]
    await record_delivery_progress(session, board)
    point = (await delivery_progress_history(session, delivery.id))[0]
    assert point.blocked == 1
    assert point.awaiting_signoff == 0
    assert point.open_findings == 1
    assert point.acknowledged_findings == 0
    board.tasks[0].defects[0].acknowledged = True
    await record_delivery_progress(session, board)
    point = (await delivery_progress_history(session, delivery.id))[0]
    assert point.open_findings == 0
    assert point.acknowledged_findings == 1


@pytest.mark.asyncio
async def test_background_recorder_samples_without_a_page_read(monkeypatch):
    from contextlib import asynccontextmanager
    from sqlalchemy.ext.asyncio import AsyncSession
    import oddish.db.connection as connection
    import oddish.core.delivery_progress as progress

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

        monkeypatch.setattr(progress, "get_read_session", get_session)
        monkeypatch.setattr(progress, "get_session", get_session)
        try:
            async with get_session() as session:
                delivery = await create_delivery_core(
                    session,
                    org_id=ORG,
                    user_id="reviewer",
                    data=DeliveryCreate(
                        name="background-progress", customer="customer"
                    ),
                )
                delivery_id = delivery.id
                await session.commit()
            await progress.sample_active_deliveries()
            await progress.sample_active_deliveries()
            async with get_session() as session:
                history = await delivery_progress_history(session, delivery_id)
                assert len(history) == 1
                assert history[0].task_count == 0
                assert (
                    await session.scalar(
                        select(func.count()).select_from(DeliveryProgressModel)
                    )
                    == 1
                )
        finally:
            await transaction.rollback()
