"""Preview/apply of a delivery metadata plan against real PostgreSQL rows."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from test_delivery_backfill import bundle

from oddish.core.endpoints.deletion import delete_task_core
from oddish.core.ingest.delivery_apply import (
    apply_plan_core,
    list_import_receipts_core,
    parse_customer_map,
)
from oddish.core.ingest.delivery_backfill import build_plan
from oddish.core.ingest.delivery_inventory import (
    INVENTORY_SCHEMA,
    export_inventory_core,
)
from oddish.db import (
    CustomerModel,
    MetadataImportReceiptModel,
    TaskAliasModel,
    TaskDeliveryHistoryModel,
    TaskMetadataAssertionModel,
    TaskModel,
    TaskSourceRecordModel,
)

ORG = "org-apply"
CAPTURED = "2026-09-16T12:00:00Z"
NOW = datetime(2026, 9, 16, 13, 0, tzinfo=timezone.utc)


def _inventory(task: TaskModel, org: str = ORG, captured: str = CAPTURED) -> dict:
    return {
        "schema_version": INVENTORY_SCHEMA,
        "org_id": org,
        "captured_at": captured,
        "tasks": [{"id": task.id, "org_id": org, "name": task.name, "categories": []}],
    }


async def _live_task(session, task_id: str = "task-1", name: str = "new-name"):
    task = TaskModel(
        id=task_id, name=name, org_id=ORG, user="tester", task_path=f"s3://t/{name}"
    )
    session.add(task)
    await session.flush()
    return task


async def _count(session, model, **where):
    query = select(func.count()).select_from(model)
    for key, value in where.items():
        query = query.where(getattr(model, key) == value)
    return await session.scalar(query)


async def _apply(session, plan, inventory, *, preview_first=True, **kw):
    """Run the plan; an apply is preceded by the preview the server demands."""
    kw.setdefault("now", NOW)
    kw.setdefault("mode", "apply")
    kw.setdefault("org_id", ORG)
    if kw["mode"] == "apply" and preview_first:
        await apply_plan_core(
            session, plan=plan, inventory=inventory, **{**kw, "mode": "preview"}
        )
    return await apply_plan_core(session, plan=plan, inventory=inventory, **kw)


@pytest.mark.asyncio
async def test_preview_records_receipt_and_writes_no_facts(session):
    task = await _live_task(session)
    inventory = _inventory(task)
    plan = build_plan(bundle(), org_id=ORG, inventory=inventory)
    receipt = await _apply(session, plan, inventory, mode="preview")
    assert receipt.outcome == "previewed"
    assert receipt.summary["source_records"] == {
        "created": 4,
        "updated": 0,
        "unchanged": 0,
    }
    assert receipt.summary["aliases"] == {"created": 1, "updated": 0, "unchanged": 0}
    assert receipt.summary["delivery_history"]["created"] == 1
    for model in (
        TaskSourceRecordModel,
        TaskAliasModel,
        TaskMetadataAssertionModel,
        TaskDeliveryHistoryModel,
    ):
        assert await _count(session, model, org_id=ORG) == 0, model.__tablename__
    assert await _count(session, MetadataImportReceiptModel, id=receipt.id) == 1


@pytest.mark.asyncio
async def test_apply_writes_facts_and_replay_updates_the_same_rows(session):
    task = await _live_task(session)
    inventory = _inventory(task)
    plan = build_plan(bundle(), org_id=ORG, inventory=inventory)
    first = await _apply(session, plan, inventory)
    assert first.outcome == "applied"
    assert first.summary["source_records"]["created"] == 4
    alias = await session.scalar(
        select(TaskAliasModel).where(TaskAliasModel.org_id == ORG)
    )
    assert (alias.task_id, alias.name, alias.import_id) == (
        task.id,
        "old-name",
        first.id,
    )
    assertion = await session.scalar(
        select(TaskMetadataAssertionModel).where(
            TaskMetadataAssertionModel.org_id == ORG
        )
    )
    assert (assertion.field, assertion.value) == ("category", "Migration")
    history = await session.scalar(
        select(TaskDeliveryHistoryModel).where(TaskDeliveryHistoryModel.org_id == ORG)
    )
    assert (history.customer_label, history.batch, history.task_id) == (
        "meta",
        "meta_2026-07-28",
        task.id,
    )
    assert history.customer_id is None
    assert history.shipped_version_id is None
    assert history.customer_acceptance == "unknown"

    # Same plan again: nothing new. Facts keep the receipt that wrote them;
    # source records record the receipt that last saw them.
    second = await _apply(session, plan, inventory)
    assert second.outcome == "applied"
    assert second.summary["source_records"] == {
        "created": 0,
        "updated": 0,
        "unchanged": 4,
    }
    assert second.summary["aliases"] == {"created": 0, "updated": 0, "unchanged": 1}
    assert second.summary["assertions"] == {
        "created": 0,
        "updated": 0,
        "unchanged": 1,
    }
    assert second.summary["delivery_history"]["unchanged"] == 1
    await session.refresh(alias)
    assert alias.import_id == first.id
    assert await _count(session, TaskSourceRecordModel, org_id=ORG) == 4
    assert await _count(session, TaskAliasModel, org_id=ORG) == 1
    assert await _count(session, TaskDeliveryHistoryModel, org_id=ORG) == 1
    last_imports = set(
        (
            await session.scalars(
                select(TaskSourceRecordModel.last_import_id).where(
                    TaskSourceRecordModel.org_id == ORG
                )
            )
        ).all()
    )
    assert last_imports == {second.id}
    first_imports = set(
        (
            await session.scalars(
                select(TaskSourceRecordModel.first_import_id).where(
                    TaskSourceRecordModel.org_id == ORG
                )
            )
        ).all()
    )
    assert first_imports == {first.id}


@pytest.mark.asyncio
async def test_changed_source_value_updates_record_in_place(session):
    task = await _live_task(session)
    inventory = _inventory(task)
    await _apply(
        session, build_plan(bundle(), org_id=ORG, inventory=inventory), inventory
    )
    edited = bundle()
    edited["pass_rate_rows"][0]["delivered"] = "no"
    receipt = await _apply(
        session, build_plan(edited, org_id=ORG, inventory=inventory), inventory
    )
    assert receipt.summary["source_records"] == {
        "created": 0,
        "updated": 1,
        "unchanged": 3,
    }
    row = await session.scalar(
        select(TaskSourceRecordModel).where(
            TaskSourceRecordModel.org_id == ORG,
            TaskSourceRecordModel.kind == "pass_rates",
        )
    )
    assert row.facts["delivered"] == "no"
    assert row.last_import_id == receipt.id


@pytest.mark.asyncio
async def test_static_rejections_write_receipt_only(session):
    task = await _live_task(session)
    inventory = _inventory(task)
    plan = build_plan(bundle(), org_id=ORG, inventory=inventory)

    other = await _apply(session, plan, inventory, org_id="org-other")
    assert other.outcome == "rejected" and "organization" in other.rejection_reason

    foreign = _inventory(task)
    foreign["tasks"][0]["categories"] = ["Security"]
    receipt = await _apply(session, plan, foreign)
    assert receipt.outcome == "rejected"
    assert "not the one the plan was built from" in receipt.rejection_reason

    receipt = await _apply(session, plan, inventory, now=NOW + timedelta(days=8))
    assert receipt.outcome == "rejected" and "older than" in receipt.rejection_reason

    no_inventory = build_plan(bundle(), org_id=ORG, inventory=None)
    receipt = await _apply(session, no_inventory, None)
    assert receipt.outcome == "rejected" and "without an organization inventory" in (
        receipt.rejection_reason
    )
    # Each rejected apply also left its (rejected) preview receipt behind.
    assert await _count(session, MetadataImportReceiptModel, org_id=ORG) == 6
    assert await _count(session, MetadataImportReceiptModel, org_id="org-other") == 2


@pytest.mark.asyncio
async def test_identity_drift_since_inventory_rejects_plan(session):
    task = await _live_task(session)
    inventory = _inventory(task)
    plan = build_plan(bundle(), org_id=ORG, inventory=inventory)

    task.name = "renamed-since-inventory"
    await session.flush()
    receipt = await _apply(session, plan, inventory, mode="preview")
    assert receipt.outcome == "rejected"
    assert receipt.summary["problems"] == [
        "task task-1 is now named 'renamed-since-inventory'; the inventory said 'new-name'"
    ]
    task.name = "new-name"
    await session.flush()

    # The proposed alias is now another live task's current name.
    squatter = await _live_task(session, task_id="task-2", name="old-name")
    receipt = await _apply(session, plan, inventory, mode="preview")
    assert receipt.outcome == "rejected"
    assert receipt.summary["problems"] == [
        "alias 'old-name' for task task-1 is the current name of task task-2"
    ]
    squatter.name = "something-else"
    await session.flush()

    # An earlier import gave that alias to a different task.
    session.add(TaskAliasModel(org_id=ORG, task_id=squatter.id, name="old-name"))
    await session.flush()
    receipt = await _apply(session, plan, inventory, mode="preview")
    assert receipt.outcome == "rejected"
    assert receipt.summary["problems"] == [
        "alias 'old-name' for task task-1 already belongs to task task-2"
    ]
    assert await _count(session, TaskDeliveryHistoryModel, org_id=ORG) == 0


@pytest.mark.asyncio
async def test_customer_map_links_history_only_to_existing_customers(session):
    task = await _live_task(session)
    inventory = _inventory(task)
    plan = build_plan(bundle(), org_id=ORG, inventory=inventory)
    receipt = await _apply(
        session, plan, inventory, mode="preview", customer_map={"meta": "Meta"}
    )
    assert receipt.outcome == "rejected"
    assert "customer 'Meta' for source label 'meta' does not exist" in (
        receipt.rejection_reason
    )

    customer = CustomerModel(org_id=ORG, name="Meta")
    session.add(customer)
    await session.flush()
    receipt = await _apply(session, plan, inventory, customer_map={"meta": "Meta"})
    assert receipt.outcome == "applied"
    assert receipt.summary["delivery_history"]["customer_labels_unmapped"] == []
    history = await session.scalar(
        select(TaskDeliveryHistoryModel).where(TaskDeliveryHistoryModel.org_id == ORG)
    )
    assert history.customer_id == customer.id

    # A replay without the mapping keeps the confirmed customer.
    receipt = await _apply(session, plan, inventory)
    assert receipt.summary["delivery_history"]["customer_labels_unmapped"] == ["meta"]
    assert receipt.summary["delivery_history"]["unchanged"] == 1
    await session.refresh(history)
    assert history.customer_id == customer.id


@pytest.mark.asyncio
async def test_unresolved_observations_are_counted_not_invented(session):
    task = await _live_task(session, task_id="task-9", name="unrelated")
    inventory = _inventory(task)
    # The bundle's explicit ID task-1 is absent from this inventory, so no
    # profile resolves; evidence is still retained.
    plan = build_plan(bundle(), org_id=ORG, inventory=inventory)
    assert plan["summary"]["resolved_delivery_observations"] == 0
    receipt = await _apply(session, plan, inventory)
    assert receipt.outcome == "applied"
    assert receipt.summary["resolved_profiles"] == 0
    assert receipt.summary["delivery_history"] == {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_unresolved": 1,
        "customer_labels_unmapped": [],
    }
    assert await _count(session, TaskSourceRecordModel, org_id=ORG) == 4
    assert await _count(session, TaskDeliveryHistoryModel, org_id=ORG) == 0
    assert await _count(session, TaskAliasModel, org_id=ORG) == 0


@pytest.mark.asyncio
async def test_retiring_a_task_keeps_imported_history(session):
    task = await _live_task(session)
    inventory = _inventory(task)
    plan = build_plan(bundle(), org_id=ORG, inventory=inventory)
    await _apply(session, plan, inventory)
    await delete_task_core(session, task_id=task.id, org_id=ORG)
    session.expire_all()
    assert (
        await session.scalar(select(TaskModel).where(TaskModel.id == "task-1")) is None
    )
    assert await _count(session, TaskDeliveryHistoryModel, task_id="task-1") == 1
    assert await _count(session, TaskAliasModel, task_id="task-1") == 1
    assert await _count(session, TaskMetadataAssertionModel, task_id="task-1") == 1
    assert await _count(session, TaskSourceRecordModel, org_id=ORG) == 4


def test_parse_customer_map():
    assert parse_customer_map(["meta=Meta", " xai = xAI "]) == {
        "meta": "Meta",
        "xai": "xAI",
    }
    with pytest.raises(ValueError):
        parse_customer_map(["meta"])
    with pytest.raises(ValueError):
        parse_customer_map(["meta=Meta", "meta=Other"])


@pytest.mark.asyncio
async def test_standalone_rows_use_the_local_label(session):
    task = TaskModel(
        id="task-1", name="new-name", org_id=None, user="t", task_path="s3://t"
    )
    session.add(task)
    await session.flush()
    inventory = await export_inventory_core(session, org_id=None)
    assert inventory["org_id"] == "local"
    plan = build_plan(bundle(), org_id="local", inventory=inventory)
    await apply_plan_core(session, plan=plan, inventory=inventory, org_id=None)
    receipt = await apply_plan_core(
        session, plan=plan, inventory=inventory, org_id=None, mode="apply"
    )
    assert receipt.outcome == "applied", receipt.summary
    assert receipt.org_id == "local"
    assert await _count(session, TaskDeliveryHistoryModel, org_id="local") == 1
    receipts = await list_import_receipts_core(session, org_id=None)
    assert [(r.mode, r.outcome) for r in receipts] == [
        ("apply", "applied"),
        ("preview", "previewed"),
    ]
    assert await list_import_receipts_core(session, org_id=ORG) == []


@pytest.mark.asyncio
async def test_apply_requires_a_recent_preview_of_the_same_plan(session):
    task = await _live_task(session)
    inventory = _inventory(task)
    plan = build_plan(bundle(), org_id=ORG, inventory=inventory)
    receipt = await _apply(session, plan, inventory, preview_first=False)
    assert receipt.outcome == "rejected"
    assert "requires a preview" in receipt.rejection_reason
    assert await _count(session, TaskSourceRecordModel, org_id=ORG) == 0

    # A preview of a different plan does not count.
    other_bundle = bundle()
    other_bundle["summary"]["as_of_date"] = "2026-09-11"
    other = build_plan(other_bundle, org_id=ORG, inventory=inventory)
    await _apply(session, other, inventory, mode="preview")
    receipt = await _apply(session, plan, inventory, preview_first=False)
    assert receipt.outcome == "rejected"

    # An old preview does not count either.
    await _apply(session, plan, inventory, mode="preview", now=NOW - timedelta(days=2))
    receipt = await _apply(session, plan, inventory, preview_first=False)
    assert receipt.outcome == "rejected"

    await _apply(session, plan, inventory, mode="preview")
    receipt = await _apply(session, plan, inventory, preview_first=False)
    assert receipt.outcome == "applied", receipt.summary
