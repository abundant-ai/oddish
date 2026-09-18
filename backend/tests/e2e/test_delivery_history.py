"""Delivery metadata backfill through the real CLI: inventory, planner,
preview, apply, receipts. Every write goes through the hosted API."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from sqlalchemy import select

from .conftest import DB_URL, E2E_ENABLED, cli

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not (E2E_ENABLED and DB_URL),
        reason="e2e opt-in: set ODDISH_E2E=1 and ODDISH_DATABASE_URL",
    ),
]


def _bundle(task_id: str, as_of: str = "2026-09-10") -> dict:
    """The smallest research bundle that names the seeded task by explicit ID
    and records one old name, one category, and one customer batch."""
    return {
        "summary": {"as_of_date": as_of, "repository_revision": "a" * 40},
        "sources": [
            {"path": path, "source_url": f"https://example.org/{path}"}
            for path in [
                "taskboard/data/taskboard.json",
                "taskboard/data/oddish_pass_rates.csv",
                "deliveries/2026-09-03-gdm/rework_tracker.csv",
            ]
        ],
        "ledger_tasks": {
            task_id: {
                "prev_name": f"{task_id}-old",
                "status": "delivered",
                "updated_by": "A Teammate",
            }
        },
        "shipments": [
            {
                "task": task_id,
                "source_task_name": task_id,
                "customer": "lab",
                "batch": "lab_2026-07-28",
                "delivery_date_from_source": "2026-07-28",
                "delivery_name": task_id,
                "oddish_task_id": task_id,
                "membership": "current source record",
                "source_urls": ["https://example.org/catalog"],
            }
        ],
        "pass_rate_rows": [
            {
                "task": f"{task_id}-old",
                "old_names": "",
                "category": "Migration",
                "task_versions": "1",
                "delivered": "yes",
            }
        ],
        "gdm_rework_rows": [],
    }


def _plan(tmp_path, bundle: dict, org_id: str, inventory_path):
    bundle_path = tmp_path / f"bundle-{bundle['summary']['as_of_date']}.json"
    bundle_path.write_text(json.dumps(bundle))
    out = tmp_path / f"preview-{bundle['summary']['as_of_date']}"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "oddish.core.ingest.delivery_backfill",
            "--bundle",
            str(bundle_path),
            "--org-id",
            org_id,
            "--inventory",
            str(inventory_path),
            "--output-dir",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return out / "plan.json", out / "evidence-raw.json"


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


async def test_inventory_plan_preview_apply_and_receipts(live_server, seeded, tmp_path):
    from oddish.db import TaskAliasModel, TaskDeliveryHistoryModel, get_session

    task_id, org_id, key = seeded["task_id"], seeded["org_id"], seeded["api_key"]

    inventory_path = tmp_path / "inventory.json"
    proc = cli(
        live_server, key, "delivery", "inventory", "--output", str(inventory_path)
    )
    assert proc.returncode == 0, proc.stderr
    inventory = json.loads(inventory_path.read_text())
    assert inventory["org_id"] == org_id
    assert [t["id"] for t in inventory["tasks"]] == [task_id]

    plan_path, raw_path = _plan(tmp_path, _bundle(task_id), org_id, inventory_path)
    # Names and notes stay in the local raw file, never in the upload.
    assert "A Teammate" in raw_path.read_text()
    assert "A Teammate" not in plan_path.read_text()

    def import_history(*extra: str) -> tuple[int, dict | None]:
        proc = cli(
            live_server,
            key,
            "delivery",
            "import-history",
            str(plan_path),
            "--inventory",
            str(inventory_path),
            "--json",
            *extra,
        )
        return proc.returncode, (
            json.loads(proc.stdout) if proc.stdout.strip() else None
        )

    # Apply before any preview is refused by the server, with a receipt.
    code, receipt = import_history("--apply")
    assert code == 3, receipt
    assert receipt["outcome"] == "rejected"
    assert "requires a preview" in receipt["rejection_reason"]

    code, receipt = import_history()
    assert code == 0, receipt
    assert (receipt["mode"], receipt["outcome"]) == ("preview", "previewed")
    assert receipt["summary"]["resolved_profiles"] == 1
    assert receipt["summary"]["aliases"]["created"] == 1
    assert receipt["summary"]["delivery_history"]["created"] == 1
    async with get_session() as session:
        assert (
            await session.scalar(
                select(TaskAliasModel).where(TaskAliasModel.task_id == task_id)
            )
        ) is None

    code, receipt = import_history("--apply")
    assert code == 0, receipt
    assert (receipt["mode"], receipt["outcome"]) == ("apply", "applied")
    async with get_session() as session:
        alias = await session.scalar(
            select(TaskAliasModel).where(TaskAliasModel.task_id == task_id)
        )
        assert alias is not None and alias.name == f"{task_id}-old"
        history = (
            await session.scalars(
                select(TaskDeliveryHistoryModel).where(
                    TaskDeliveryHistoryModel.task_id == task_id
                )
            )
        ).all()
        assert [(h.customer_label, h.batch) for h in history] == [
            ("lab", "lab_2026-07-28")
        ]

    # Replaying the same plan changes nothing.
    code, receipt = import_history("--apply")
    assert code == 0 and receipt["outcome"] == "applied"
    assert receipt["summary"]["delivery_history"] == {
        "created": 0,
        "updated": 0,
        "unchanged": 1,
        "skipped_unresolved": 0,
        "customer_labels_unmapped": ["lab"],
    }

    proc = cli(live_server, key, "delivery", "import-receipts", "--json")
    assert proc.returncode == 0, proc.stderr
    receipts = json.loads(proc.stdout)
    assert [(r["mode"], r["outcome"]) for r in receipts] == [
        ("apply", "applied"),
        ("apply", "applied"),
        ("preview", "previewed"),
        ("apply", "rejected"),
    ]
