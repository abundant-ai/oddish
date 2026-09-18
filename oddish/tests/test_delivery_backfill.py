"""Offline identity/backfill regressions; runnable with unittest without a DB."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from oddish.core.ingest.delivery_backfill import (
    build_plan,
    collect_evidence,
    compare_plans,
    task_id_from_url,
)
from oddish.core.ingest.delivery_inventory import INVENTORY_SCHEMA, inventory_task


def bundle():
    return {
        "summary": {"as_of_date": "2026-09-10", "repository_revision": "a" * 40},
        "sources": [
            {"path": path, "source_url": f"https://example.org/{path}"}
            for path in [
                "taskboard/data/taskboard.json",
                "taskboard/data/oddish_pass_rates.csv",
                "deliveries/2026-09-03-gdm/rework_tracker.csv",
            ]
        ],
        "ledger_tasks": {"new-name": {"prev_name": "old-name", "status": "returned"}},
        "shipments": [
            {
                "task": "new-name",
                "source_task_name": "new-name",
                "customer": "meta",
                "batch": "meta_2026-07-28",
                "delivery_date_from_source": "2026-07-28",
                "delivery_name": "customer-name",
                "oddish_task_id": "task-1",
                "membership": "current source record",
                "source_urls": ["https://example.org/catalog"],
                "ledger_status_task_wide": "returned",
            }
        ],
        "pass_rate_rows": [
            {
                "task": "old-name",
                "old_names": "",
                "category": "Migration",
                "task_versions": "1;2;7",
                "delivered": "yes",
                "oracle": "4/4",
            }
        ],
        "gdm_rework_rows": [
            {
                "task": "new-name",
                "category": "Migration",
                "Task url": "https://www.oddish.app/tasks/task-1",
                "Current version": "7",
                "Status": "ALL PASSING",
            }
        ],
    }


def inventory():
    return {
        "schema_version": INVENTORY_SCHEMA,
        "org_id": "org-1",
        "captured_at": "2026-09-16T12:00:00Z",
        "tasks": [
            {
                "id": "task-1",
                "org_id": "org-1",
                "name": "new-name",
                "categories": [],
                "current_version_id": "task-1-v99",
                "current_content_hash": "current-hash",
            },
        ],
    }


class DeliveryBackfillTests(unittest.TestCase):
    def test_explicit_id_and_rename_preserve_names_and_sources(self):
        plan = build_plan(bundle(), org_id="org-1", inventory=inventory())
        profile = plan["task_profiles"][0]
        self.assertEqual(profile["task_id"], "task-1")
        self.assertEqual(profile["names"], ["new-name", "old-name"])
        self.assertEqual(profile["category_status"], "proposed_addition")
        self.assertEqual(len(plan["metadata_proposals"]), 2)
        evidence = {r["record_id"]: r for r in plan["evidence"]}
        for proposal in plan["metadata_proposals"]:
            self.assertTrue(
                all(evidence[e]["source_urls"] for e in proposal["evidence_ids"])
            )

    def test_source_id_requires_inventory(self):
        plan = build_plan(bundle(), org_id="org-1")
        self.assertEqual(
            plan["task_profiles"][0]["identity_status"], "source_id_unverified"
        )
        self.assertEqual(plan["metadata_proposals"], [])
        self.assertIsNone(plan["delivery_observations"][0]["task_id"])

    def test_absent_source_id_does_not_fall_back_to_same_name(self):
        inv = inventory()
        inv["tasks"][0]["id"] = "different-id"
        plan = build_plan(bundle(), org_id="org-1", inventory=inv)
        self.assertEqual(
            plan["task_profiles"][0]["identity_status"],
            "source_id_absent_from_inventory",
        )
        self.assertEqual(plan["metadata_proposals"], [])

    def test_conflicting_ids_quarantine_entire_alias_group(self):
        data = bundle()
        data["gdm_rework_rows"][0]["Task url"] = "https://oddish.app/tasks/task-2"
        plan = build_plan(data, org_id="org-1", inventory=inventory())
        self.assertEqual(
            plan["task_profiles"][0]["identity_status"], "conflicting_explicit_ids"
        )
        self.assertEqual(
            plan["task_profiles"][0]["explicit_task_ids"], ["task-1", "task-2"]
        )
        self.assertEqual(plan["metadata_proposals"], [])

    def test_reused_old_name_does_not_claim_another_task(self):
        inv = inventory()
        inv["tasks"].append(
            {"id": "task-2", "name": "old-name", "org_id": "org-1", "categories": []}
        )
        plan = build_plan(bundle(), org_id="org-1", inventory=inv)
        self.assertEqual(plan["task_profiles"][0]["identity_status"], "name_collision")
        self.assertEqual(plan["metadata_proposals"], [])

    def test_name_only_is_candidate_not_resolution(self):
        data = bundle()
        data["shipments"][0]["oddish_task_id"] = None
        data["gdm_rework_rows"][0]["Task url"] = ""
        plan = build_plan(data, org_id="org-1", inventory=inventory())
        self.assertEqual(
            plan["task_profiles"][0]["identity_status"], "name_match_needs_review"
        )
        self.assertEqual(plan["task_profiles"][0]["candidate_task_ids"], ["task-1"])
        self.assertEqual(plan["metadata_proposals"], [])

    def test_cross_org_inventory_and_rows_are_rejected(self):
        for change_header in [True, False]:
            with self.subTest(header=change_header):
                inv = inventory()
                if change_header:
                    inv["org_id"] = "org-2"
                else:
                    inv["tasks"][0]["org_id"] = "org-2"
                with self.assertRaisesRegex(ValueError, "organization"):
                    build_plan(bundle(), org_id="org-1", inventory=inv)

    def test_duplicate_inventory_ids_are_rejected(self):
        inv = inventory()
        inv["tasks"].append(deepcopy(inv["tasks"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate inventory"):
            build_plan(bundle(), org_id="org-1", inventory=inv)

    def test_exact_replay_has_no_new_or_changed_evidence(self):
        first = build_plan(bundle(), org_id="org-1", inventory=inventory())
        second = build_plan(bundle(), org_id="org-1", inventory=inventory())
        self.assertEqual(first, second)
        comparison = compare_plans(first, second)
        self.assertEqual(comparison["added"], [])
        self.assertEqual(comparison["changed"], [])
        self.assertEqual(len(comparison["unchanged"]), len(first["evidence"]))

    def test_reordered_rows_keep_ids_and_proposals(self):
        data = bundle()
        other = deepcopy(data["shipments"][0])
        other["customer"], other["batch"] = "xai", "xai_2026-08-01"
        data["shipments"].append(other)
        first = build_plan(data, org_id="org-1", inventory=inventory())
        data["shipments"].reverse()
        second = build_plan(data, org_id="org-1", inventory=inventory())
        for key in [
            "evidence",
            "metadata_proposals",
            "task_profiles",
            "delivery_observations",
        ]:
            self.assertEqual(first[key], second[key])

    def test_changed_value_updates_same_source_identity(self):
        data = bundle()
        first = build_plan(data, org_id="org-1")
        data["pass_rate_rows"][0]["category"] = "Debugging"
        second = build_plan(data, org_id="org-1")
        diff = compare_plans(first, second)
        self.assertEqual(diff["added"], [])
        self.assertEqual(len(diff["changed"]), 1)
        self.assertEqual(second["task_profiles"][0]["category_status"], "conflict")

    def test_removed_source_is_review_item_not_history_deletion(self):
        data = bundle()
        first = build_plan(data, org_id="org-1")
        data["shipments"] = []
        diff = compare_plans(first, build_plan(data, org_id="org-1"))
        self.assertEqual(len(diff["missing_from_new_source_requires_review"]), 1)

    def test_category_conflict_never_overwrites_existing_value(self):
        inv = inventory()
        inv["tasks"][0]["categories"] = ["Debugging"]
        plan = build_plan(bundle(), org_id="org-1", inventory=inv)
        self.assertEqual(plan["task_profiles"][0]["category_status"], "conflict")
        self.assertFalse(
            any(p["field"] == "category" for p in plan["metadata_proposals"])
        )

    def test_existing_category_is_noop(self):
        inv = inventory()
        inv["tasks"][0]["categories"] = ["Migration"]
        plan = build_plan(bundle(), org_id="org-1", inventory=inv)
        self.assertEqual(
            plan["task_profiles"][0]["category_status"], "already_recorded"
        )
        self.assertFalse(
            any(p["field"] == "category" for p in plan["metadata_proposals"])
        )

    def test_current_versions_and_passing_qa_do_not_become_delivery_facts(self):
        plan = build_plan(bundle(), org_id="org-1", inventory=inventory())
        observation = plan["delivery_observations"][0]
        for field in [
            "shipped_version_id",
            "shipped_content_hash",
            "program",
            "finalized_at",
        ]:
            self.assertIsNone(observation[field])
        self.assertEqual(observation["customer_acceptance"], "unknown")
        self.assertEqual(plan["task_profiles"][0]["history_coverage"], "partial")
        self.assertEqual(observation["customer_task_name"], "customer-name")

    def test_same_task_different_labs_keep_separate_observations(self):
        data = bundle()
        other = deepcopy(data["shipments"][0])
        other["customer"], other["batch"] = "xai", "xai_2026-08-01"
        data["shipments"].append(other)
        plan = build_plan(data, org_id="org-1", inventory=inventory())
        self.assertEqual(len(plan["delivery_observations"]), 2)
        self.assertEqual(
            plan["task_profiles"][0]["recorded_recipients"], ["meta", "xai"]
        )

    def test_duplicate_source_replay_collapses_but_conflicting_duplicate_fails(self):
        data = bundle()
        original_count = len(collect_evidence(data))
        data["shipments"].append(deepcopy(data["shipments"][0]))
        self.assertEqual(len(collect_evidence(data)), original_count)
        data["shipments"][-1]["membership"] = "different"
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            collect_evidence(data)

    def test_unrelated_url_is_not_an_oddish_identity(self):
        for url in [
            "https://evil.example/tasks/task-1",
            "https://oddish.app.evil.example/tasks/task-1",
            "http://oddish.app/tasks/task-1",
            "https://oddish.app/experiments/task-1",
        ]:
            self.assertIsNone(task_id_from_url(url))
        self.assertEqual(
            task_id_from_url("https://oddish.app/tasks/task-1?version=2"), "task-1"
        )

    def test_same_explicit_id_links_names_without_an_alias(self):
        data = bundle()
        data["gdm_rework_rows"][0]["task"] = "different-name"
        plan = build_plan(data, org_id="org-1", inventory=inventory())
        self.assertEqual(len(plan["task_profiles"]), 1)
        self.assertIn("different-name", plan["task_profiles"][0]["names"])

    def test_similar_names_do_not_merge(self):
        data = bundle()
        data["pass_rate_rows"].append(
            {"task": "new-name-v2", "old_names": "", "category": "Migration"}
        )
        plan = build_plan(data, org_id="org-1", inventory=inventory())
        self.assertEqual(len(plan["task_profiles"]), 2)
        self.assertEqual(sum(p["task_id"] is None for p in plan["task_profiles"]), 1)

    def test_previous_plan_cannot_cross_organizations(self):
        with self.assertRaisesRegex(ValueError, "same schema and organization"):
            compare_plans(
                build_plan(bundle(), org_id="org-2"),
                build_plan(bundle(), org_id="org-1"),
            )

    def test_nexus_acceptance_remains_separate_from_customer_acceptance(self):
        data = bundle()
        data["nexus_submission_records"] = [
            {
                "task": "new-name",
                "source_task_name": "new-name",
                "pull_request": 755,
                "project_id": "P-123",
                "source_url": "https://example.org/comment/123",
                "ingestion_result": "payload accepted by Nexus",
            }
        ]
        data["nexus_blocked_tasks"] = [
            {
                "task": "new-name",
                "project_id": "P-123",
                "status": "BLOCKED",
                "source_url": "https://example.org/validation/123",
            }
        ]
        plan = build_plan(data, org_id="org-1", inventory=inventory())
        self.assertEqual(len(plan["delivery_observations"]), 1)
        self.assertEqual(
            plan["delivery_observations"][0]["customer_acceptance"], "unknown"
        )
        self.assertEqual(
            sum(r["kind"].startswith("nexus_") for r in plan["evidence"]), 2
        )

    def test_missing_source_url_is_not_silently_imported(self):
        data = bundle()
        data["shipments"][0]["source_urls"] = []
        with self.assertRaisesRegex(ValueError, "missing task name or source URL"):
            build_plan(data, org_id="org-1")

    def test_retired_identity_remains_available_for_historical_linking(self):
        inv = inventory()
        inv["tasks"][0]["retired_at"] = "2026-09-01T00:00:00Z"
        plan = build_plan(bundle(), org_id="org-1", inventory=inv)
        self.assertEqual(plan["delivery_observations"][0]["task_id"], "task-1")

    def test_exporter_preserves_existing_category_evidence_and_retired_identity(self):
        row = {
            "id": "task-1",
            "name": "new-name",
            "org_id": "org-1",
            "task_path": "tasks/new-name",
            "deleted_at": datetime(2026, 9, 1, tzinfo=timezone.utc),
            "content_hash": "abc",
            "current_version_id": "v2",
            "current_version_tag_ids": ["old-tag"],
            "tags": {"category": "Migration", "github_meta": '{"category":"Rust"}'},
        }
        tags = {
            "old-tag": {"merged_into_id": "new-tag"},
            "new-tag": {
                "id": "new-tag",
                "normalized_key": "category",
                "value": "Security",
            },
        }
        result = inventory_task(row, tags)
        self.assertEqual(result["categories"], ["Migration", "Rust", "Security"])
        self.assertEqual(len(result["category_evidence"]), 3)
        self.assertTrue(result["retired_at"])

    def test_cli_writes_preview_and_refuses_to_replace_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bundle.json"
            source.write_text(json.dumps(bundle()))
            command = [
                sys.executable,
                "-m",
                "oddish.core.ingest.delivery_backfill",
                "--bundle",
                str(source),
                "--org-id",
                "org-1",
                "--output-dir",
                str(root / "preview"),
            ]
            environment = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            }
            first = subprocess.run(
                command, capture_output=True, text=True, env=environment
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            saved = (root / "preview" / "plan.json").read_bytes()
            second = subprocess.run(
                command, capture_output=True, text=True, env=environment
            )
            self.assertEqual(second.returncode, 2)
            self.assertEqual((root / "preview" / "plan.json").read_bytes(), saved)
            self.assertIn(
                "No Oddish records", (root / "preview" / "README.md").read_text()
            )

    def test_plan_carries_facts_and_raw_rows_stay_local(self):
        data = bundle()
        data["ledger_tasks"]["new-name"]["updated_by"] = "A Teammate"
        data["ledger_tasks"]["new-name"]["notes"] = "private note"
        raw = {}
        plan = build_plan(data, org_id="org-1", raw_sink=raw)
        ledger = next(r for r in plan["evidence"] if r["kind"] == "ledger")
        self.assertEqual(ledger["facts"]["prev_name"], "old-name")
        self.assertNotIn("updated_by", ledger["facts"])
        self.assertNotIn("raw", ledger)
        self.assertNotIn("A Teammate", json.dumps(plan))
        self.assertEqual(raw[ledger["record_id"]]["updated_by"], "A Teammate")
        # A change outside the fact allowlist is not a source change.
        data["ledger_tasks"]["new-name"]["notes"] = "edited note"
        self.assertEqual(
            compare_plans(plan, build_plan(data, org_id="org-1"))["changed"], []
        )


if __name__ == "__main__":
    unittest.main()
