"""Delivery metadata inventory export through the real CLI."""

from __future__ import annotations

import json

import pytest

from .conftest import DB_URL, E2E_ENABLED, cli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (E2E_ENABLED and DB_URL),
        reason="e2e opt-in: set ODDISH_E2E=1 and ODDISH_DATABASE_URL",
    ),
]


async def test_inventory_exports_the_seeded_task(live_server, seeded, tmp_path):
    inventory_path = tmp_path / "inventory.json"
    proc = cli(
        live_server,
        seeded["api_key"],
        "delivery",
        "inventory",
        "--output",
        str(inventory_path),
    )
    assert proc.returncode == 0, proc.stderr
    inventory = json.loads(inventory_path.read_text())
    assert inventory["org_id"] == seeded["org_id"]
    assert [t["id"] for t in inventory["tasks"]] == [seeded["task_id"]]
    # The file is never overwritten.
    proc = cli(
        live_server,
        seeded["api_key"],
        "delivery",
        "inventory",
        "--output",
        str(inventory_path),
    )
    assert proc.returncode != 0 and "already exists" in proc.stderr
