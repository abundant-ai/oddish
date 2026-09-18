"""Uniqueness and retention rules of the source-backed task history tables."""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from oddish.db import (
    MetadataImportReceiptModel,
    TaskAliasModel,
    TaskDeliveryHistoryModel,
    TaskMetadataAssertionModel,
    TaskModel,
    TaskSourceRecordModel,
    generate_id,
    utcnow,
)

ROOT = Path(__file__).resolve().parents[1]
ORG = "org-history"


def test_task_history_migration_keeps_one_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert len(scripts.get_heads()) == 1


def _task(name: str, org: str = ORG) -> TaskModel:
    return TaskModel(name=name, org_id=org, user="tester", task_path=f"s3://t/{name}")


def _receipt(org: str = ORG) -> MetadataImportReceiptModel:
    return MetadataImportReceiptModel(
        org_id=org,
        plan_schema="oddish-delivery-backfill-plan-v1",
        plan_hash="0" * 64,
        mode="apply",
        outcome="applied",
    )


def _record(receipt, org: str = ORG, record_id: str | None = None):
    return TaskSourceRecordModel(
        org_id=org,
        record_id=record_id or generate_id(),
        kind="delivery_membership",
        source_key=["lab", "lab_2026-01-01", "task-a"],
        names=["task-a"],
        explicit_task_ids=[],
        source_urls=["https://example.invalid/source"],
        facts={"customer": "lab", "batch": "lab_2026-01-01"},
        content_hash="1" * 64,
        first_import_id=receipt.id,
        last_import_id=receipt.id,
    )


def _history(task, record, receipt, **kw) -> TaskDeliveryHistoryModel:
    values = dict(
        id=generate_id(),
        org_id=record.org_id,
        task_id=task.id,
        source_record_id=record.record_id,
        customer_label="lab",
        batch="lab_2026-01-01",
        membership="current source record",
        import_id=receipt.id,
    )
    values.update(kw)
    return TaskDeliveryHistoryModel(**values)


async def _expect_integrity_error(session, *rows):
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add_all(rows)
            await session.flush()


@pytest.mark.asyncio
async def test_receipt_rejects_unknown_mode_and_outcome(session):
    bad = _receipt()
    bad.outcome = "guessed"
    await _expect_integrity_error(session, bad)
    bad = _receipt()
    bad.mode = "dry-run"
    await _expect_integrity_error(session, bad)


@pytest.mark.asyncio
async def test_alias_name_identifies_one_task_per_org(session):
    first, second, other_org = (
        _task("alias-a"),
        _task("alias-b"),
        _task("alias-c", "org-2"),
    )
    session.add_all([first, second, other_org])
    await session.flush()
    session.add(TaskAliasModel(org_id=ORG, task_id=first.id, name="old-name"))
    # Same name in another organization is a different identity space.
    session.add(TaskAliasModel(org_id="org-2", task_id=other_org.id, name="old-name"))
    await session.flush()
    await _expect_integrity_error(
        session, TaskAliasModel(org_id=ORG, task_id=second.id, name="old-name")
    )
    # Re-adding the same alias to the same task is also refused: replays
    # must update the existing row, not insert a twin.
    await _expect_integrity_error(
        session, TaskAliasModel(org_id=ORG, task_id=first.id, name="old-name")
    )
    # A name the ledger reused: end the earlier alias and the name is free.
    earlier = await session.scalar(
        select(TaskAliasModel).where(
            TaskAliasModel.org_id == ORG, TaskAliasModel.name == "old-name"
        )
    )
    earlier.valid_until = utcnow()
    await session.flush()
    session.add(TaskAliasModel(org_id=ORG, task_id=second.id, name="old-name"))
    await session.flush()
    # A retracted alias frees the name too; two live ones never coexist.
    later = await session.scalar(
        select(TaskAliasModel).where(
            TaskAliasModel.org_id == ORG,
            TaskAliasModel.name == "old-name",
            TaskAliasModel.valid_until.is_(None),
        )
    )
    later.retracted_at = utcnow()
    later.retracted_by_user_id = "alice"
    await session.flush()
    session.add(TaskAliasModel(org_id=ORG, task_id=first.id, name="old-name"))
    await session.flush()


@pytest.mark.asyncio
async def test_assertions_keep_conflicting_values_but_not_duplicates(session):
    task = _task("assert-a")
    session.add(task)
    await session.flush()
    session.add_all(
        [
            TaskMetadataAssertionModel(
                org_id=ORG, task_id=task.id, field="category", value="E2e migration"
            ),
            TaskMetadataAssertionModel(
                org_id=ORG, task_id=task.id, field="category", value="Security"
            ),
        ]
    )
    await session.flush()
    await _expect_integrity_error(
        session,
        TaskMetadataAssertionModel(
            org_id=ORG, task_id=task.id, field="category", value="Security"
        ),
    )
    values = (
        await session.scalars(
            select(TaskMetadataAssertionModel.value)
            .where(TaskMetadataAssertionModel.task_id == task.id)
            .order_by(TaskMetadataAssertionModel.value)
        )
    ).all()
    assert values == ["E2e migration", "Security"]


@pytest.mark.asyncio
async def test_history_row_must_share_org_with_its_source_record(session):
    task, receipt = _task("hist-org"), _receipt()
    session.add_all([task, receipt])
    await session.flush()
    record = _record(receipt)
    session.add(record)
    await session.flush()
    await _expect_integrity_error(
        session, _history(task, record, receipt, org_id="org-2")
    )
    await _expect_integrity_error(
        session, _history(task, record, receipt, customer_acceptance="uploaded")
    )
    session.add(_history(task, record, receipt, customer_acceptance="returned"))
    await session.flush()
    # One history row per source row per organization.
    await _expect_integrity_error(session, _history(task, record, receipt))


@pytest.mark.asyncio
async def test_history_survives_task_retirement_and_blocks_hard_delete(session):
    task, receipt = _task("hist-retire"), _receipt()
    session.add_all([task, receipt])
    await session.flush()
    record = _record(receipt)
    session.add(record)
    await session.flush()
    session.add_all(
        [
            _history(task, record, receipt),
            TaskAliasModel(org_id=ORG, task_id=task.id, name="hist-retire-old"),
            TaskMetadataAssertionModel(
                org_id=ORG, task_id=task.id, field="category", value="Security"
            ),
        ]
    )
    await session.flush()

    # A hard delete is refused while history references the task.
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            await session.execute(
                delete(TaskModel)
                .where(TaskModel.id == task.id)
                .execution_options(include_deleted=True)
            )

    # Retirement is a soft delete: the task leaves normal reads, the facts stay.
    task_id = task.id
    task.deleted_at = utcnow()
    await session.flush()
    session.expire_all()
    assert (
        await session.scalar(select(TaskModel).where(TaskModel.id == task_id)) is None
    )
    retired = await session.scalar(
        select(TaskModel)
        .where(TaskModel.id == task_id)
        .execution_options(include_deleted=True)
    )
    assert retired is not None and retired.deleted_at is not None
    for model in (TaskDeliveryHistoryModel, TaskAliasModel, TaskMetadataAssertionModel):
        rows = (
            await session.scalars(select(model).where(model.task_id == task_id))
        ).all()
        assert len(rows) == 1, model.__tablename__
