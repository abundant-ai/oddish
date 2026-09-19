"""The organization-scoped task inventory export against real PostgreSQL rows."""

import pytest

from oddish.core.ingest.delivery_inventory import (
    INVENTORY_SCHEMA,
    export_inventory_core,
)
from oddish.db import TaskModel, utcnow

ORG = "org-inventory"


def _task(task_id: str, name: str, org: str | None = ORG) -> TaskModel:
    return TaskModel(id=task_id, name=name, org_id=org, user="t", task_path="s3://t")


@pytest.mark.asyncio
async def test_inventory_export_is_org_scoped_and_keeps_retired_tasks(session):
    live = _task("inv-live", "inv-live")
    retired = _task("inv-retired", "inv-retired")
    retired.deleted_at = utcnow()
    session.add_all([live, retired, _task("inv-other", "inv-other", "org-2")])
    await session.flush()
    inventory = await export_inventory_core(session, org_id=ORG)
    assert inventory["schema_version"] == INVENTORY_SCHEMA
    assert inventory["org_id"] == ORG
    by_id = {task["id"]: task for task in inventory["tasks"]}
    assert "inv-other" not in by_id
    assert by_id["inv-live"]["retired_at"] is None
    assert by_id["inv-retired"]["retired_at"] is not None
    assert all(task["org_id"] == ORG for task in by_id.values())


@pytest.mark.asyncio
async def test_standalone_rows_get_the_local_label(session):
    session.add(_task("inv-local", "inv-local", None))
    await session.flush()
    inventory = await export_inventory_core(session, org_id=None)
    assert inventory["org_id"] == "local"
    local = next(task for task in inventory["tasks"] if task["id"] == "inv-local")
    assert local["org_id"] == "local"
