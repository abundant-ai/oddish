"""Prepare source-backed delivery metadata without writing to Oddish.

Consumes the consolidated delivery-research JSON and an optional org-scoped
inventory. Run with --help for the operator interface. No network/DB imports.

The plan carries only the fields the import needs (``FACT_FIELDS``). The
verbatim source rows, which include people's names and free-text notes, are
written to a separate local file and never uploaded or stored in Oddish.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from oddish.core.ingest.delivery_inventory import INVENTORY_SCHEMA, required_text

PLAN_SCHEMA = "oddish-delivery-backfill-plan-v2"

# Source columns that become facts in Oddish, per record kind. Anything not
# listed (owners, reviewers, notes, pass counts) stays in the local raw file.
FACT_FIELDS: dict[str, tuple[str, ...]] = {
    "delivery_membership": (
        "customer",
        "batch",
        "delivery_date_from_source",
        "delivery_name",
        "membership",
        "source_task_name",
        "oddish_task_id",
        "ledger_status_task_wide",
        "gdm_repair_status",
        "gdm_version_in_tracker",
    ),
    "pass_rates": ("category", "old_names", "task_versions", "delivered", "customers"),
    "ledger": ("prev_name", "status", "type", "customers", "updated_at"),
    "removed_ledger": (
        "prev_name",
        "status",
        "type",
        "customers",
        "updated_at",
        "removal_commit",
    ),
    "gdm_rework": ("category", "Current version", "Status", "Task url"),
    "nexus_submission": (
        "customer",
        "project_id",
        "pull_request",
        "delivery_target",
        "mode",
        "ingestion_result",
        "customer_acceptance",
        "source_task_name",
    ),
    "nexus_validation": (
        "customer",
        "project_id",
        "pull_request",
        "status",
        "observed_date",
    ),
}


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


def task_id_from_url(value: str) -> str | None:
    url = urlparse(value)
    parts = url.path.strip("/").split("/")
    if (
        url.scheme == "https"
        and url.netloc in {"oddish.app", "www.oddish.app"}
        and len(parts) == 2
        and parts[0] == "tasks"
        and parts[1]
    ):
        return parts[1]
    return None


def collect_evidence(
    bundle: dict, *, raw_sink: dict[str, dict] | None = None
) -> list[dict]:
    """Keep source rows as facts, not a lossy guess at customer shipment events.

    ``raw_sink``, when given, receives the verbatim row per record ID for the
    local raw file. Records whose facts are identical collapse; records with
    the same source key but different facts fail.
    """
    sources = {s["path"]: s["source_url"] for s in bundle["sources"]}
    evidence: dict[str, dict] = {}

    def add(
        kind: str,
        key: list[str],
        row: dict,
        names: list[str],
        ids: list[str],
        urls: list[str],
    ):
        record_id = digest(["abundant-delivery-research", kind, key])
        item = {
            "record_id": record_id,
            "kind": kind,
            "source_key": key,
            "names": sorted({required_text(n, "task name") for n in names if n}),
            "explicit_task_ids": sorted(
                {required_text(i, "task ID") for i in ids if i}
            ),
            "source_urls": sorted(set(urls)),
            "facts": {k: row[k] for k in FACT_FIELDS[kind] if k in row},
        }
        if not item["names"] or not item["source_urls"]:
            raise ValueError(f"{kind} {key}: missing task name or source URL")
        item["content_hash"] = digest(item)
        if record_id in evidence and evidence[record_id] != item:
            raise ValueError(f"conflicting duplicate source key: {kind} {key}")
        evidence[record_id] = item
        if raw_sink is not None:
            raw_sink[record_id] = row

    for row in bundle["shipments"]:
        add(
            "delivery_membership",
            [row["customer"], row["batch"], row["task"]],
            row,
            [row["task"], row["source_task_name"]],
            [row.get("oddish_task_id")],
            row["source_urls"],
        )
    for row in bundle["pass_rate_rows"]:
        # The source uses a single old name; semicolons also support explicit
        # multi-alias exports. Never split on hyphens, spaces, or approximate text.
        aliases = [x.strip() for x in row.get("old_names", "").split(";") if x.strip()]
        add(
            "pass_rates",
            [row["task"]],
            row,
            [row["task"], *aliases],
            [],
            [sources["taskboard/data/oddish_pass_rates.csv"]],
        )
    for name, row in bundle["ledger_tasks"].items():
        add(
            "ledger",
            [name],
            row,
            [name, row.get("prev_name")],
            [],
            [sources["taskboard/data/taskboard.json"]],
        )
    for row in bundle["gdm_rework_rows"]:
        task_id = task_id_from_url(row.get("Task url", ""))
        add(
            "gdm_rework",
            [row["task"]],
            row,
            [row["task"]],
            [task_id],
            [sources["deliveries/2026-09-03-gdm/rework_tracker.csv"]],
        )
    for row in bundle.get("removed_ledger_tasks", []):
        # Historical deliveries carry their own pinned source URL. Retain the
        # removed record separately rather than making it disappear from identity.
        add(
            "removed_ledger",
            [row["task"]],
            row,
            [row["task"], row.get("prev_name")],
            [],
            [
                f"https://github.com/abundant-ai/harbor-lh/commit/{row['removal_commit']}"
            ],
        )
    for row in bundle.get("nexus_submission_records", []):
        add(
            "nexus_submission",
            [str(row["pull_request"]), row["project_id"], row["task"]],
            row,
            [row["task"], row["source_task_name"]],
            [],
            [row["source_url"]],
        )
    for row in bundle.get("nexus_blocked_tasks", []):
        add(
            "nexus_validation",
            [row["project_id"], row["task"]],
            row,
            [row["task"]],
            [],
            [row["source_url"]],
        )
    return sorted(evidence.values(), key=lambda r: r["record_id"])


def validate_inventory(inventory: dict | None, org_id: str) -> dict[str, dict]:
    if inventory is None:
        return {}
    if inventory.get("schema_version") != INVENTORY_SCHEMA:
        raise ValueError(f"inventory schema must be {INVENTORY_SCHEMA}")
    if inventory.get("org_id") != org_id:
        raise ValueError("inventory organization does not match --org-id")
    tasks = {}
    for row in inventory["tasks"]:
        task_id = required_text(row["id"], "inventory task ID")
        # A task with a blank name can still be matched by its explicit ID;
        # it just never takes part in name matching.
        name = row["name"]
        if not isinstance(name, str) or not name.strip():
            row = {**row, "name": ""}
        if row["org_id"] != org_id:
            raise ValueError(
                f"inventory task {task_id} belongs to another organization"
            )
        if task_id in tasks:
            raise ValueError(f"duplicate inventory task ID: {task_id}")
        categories = row["categories"]
        if not isinstance(categories, list) or any(
            not isinstance(c, str) for c in categories
        ):
            raise ValueError(
                f"inventory task {task_id}: categories must be a string list"
            )
        tasks[task_id] = row
    return tasks


def build_plan(
    bundle: dict,
    *,
    org_id: str,
    inventory: dict | None = None,
    raw_sink: dict[str, dict] | None = None,
) -> dict:
    org_id = required_text(org_id, "org_id")
    tasks = validate_inventory(inventory, org_id)
    evidence = collect_evidence(bundle, raw_sink=raw_sink)
    names_to_inventory: dict[str, set[str]] = defaultdict(set)
    for task in tasks.values():
        if task["name"]:
            names_to_inventory[task["name"]].add(task["id"])

    # Only explicitly co-recorded aliases connect names. Two records carrying
    # the same ID are also one identity, even when no rename was documented.
    adjacency: dict[str, set[str]] = defaultdict(set)
    names_by_id: dict[str, set[str]] = defaultdict(set)
    rows_by_name: dict[str, list[dict]] = defaultdict(list)
    for row in evidence:
        for name in row["names"]:
            adjacency[name].update(row["names"])
            rows_by_name[name].append(row)
        for task_id in row["explicit_task_ids"]:
            names_by_id[task_id].update(row["names"])
    for names in names_by_id.values():
        anchor = min(names)
        for name in names:
            adjacency[anchor].add(name)
            adjacency[name].add(anchor)

    profiles = []
    proposals = []
    observations = []
    seen: set[str] = set()
    for root in sorted(adjacency):
        if root in seen:
            continue
        pending = [root]
        component: set[str] = set()
        while pending:
            name = pending.pop()
            if name in component:
                continue
            component.add(name)
            pending.extend(adjacency[name] - component)
        seen.update(component)
        rows = {r["record_id"]: r for n in component for r in rows_by_name[n]}
        explicit_ids = {i for r in rows.values() for i in r["explicit_task_ids"]}
        candidate_ids = {i for n in component for i in names_to_inventory[n]}
        resolved_id = None
        if len(explicit_ids) > 1:
            resolution = "conflicting_explicit_ids"
        elif explicit_ids:
            task_id = next(iter(explicit_ids))
            if inventory is None:
                resolution = "source_id_unverified"
            elif task_id not in tasks:
                resolution = "source_id_absent_from_inventory"
            elif candidate_ids - {task_id}:
                resolution = "name_collision"
            else:
                resolution = "resolved_explicit_id"
                resolved_id = task_id
        elif candidate_ids:
            resolution = "name_match_needs_review"
        else:
            resolution = "unresolved"

        profile_id = digest(["task-profile", sorted(component)])
        category_evidence: dict[str, list[str]] = defaultdict(list)
        for row in rows.values():
            if row["kind"] in {"pass_rates", "gdm_rework"}:
                category = row["facts"].get("category", "").strip()
                if category:
                    category_evidence[category].append(row["record_id"])
        categories = sorted(category_evidence)
        existing = sorted(set(tasks[resolved_id]["categories"])) if resolved_id else []
        if len(categories) > 1 or (categories and existing and categories != existing):
            category_status = "conflict"
        elif not categories:
            category_status = "missing"
        elif existing:
            category_status = "already_recorded"
        elif resolved_id:
            category_status = "proposed_addition"
        else:
            category_status = "identity_review_required"

        profile = {
            "profile_id": profile_id,
            "names": sorted(component),
            "task_id": resolved_id,
            "explicit_task_ids": sorted(explicit_ids),
            "candidate_task_ids": sorted(candidate_ids),
            "identity_status": resolution,
            "inventory_retired_at": tasks[resolved_id].get("retired_at")
            if resolved_id
            else None,
            "category_status": category_status,
            "existing_categories": existing,
            "category_assertions": [
                {"value": c, "evidence_ids": sorted(category_evidence[c])}
                for c in categories
            ],
            "evidence_ids": sorted(rows),
            "recorded_recipients": sorted(
                {
                    r["facts"]["customer"]
                    for r in rows.values()
                    if r["kind"] == "delivery_membership"
                }
            ),
            # Absence from this collection cannot establish never delivered.
            "history_coverage": "partial",
        }
        profiles.append(profile)
        if resolved_id:
            existing_name = tasks[resolved_id]["name"]
            proposals.append(
                {
                    "proposal_id": digest([org_id, resolved_id, "source_names"]),
                    "task_id": resolved_id,
                    "field": "source_names",
                    "proposed_value": sorted(component - {existing_name}),
                    "evidence_ids": sorted(rows),
                }
            )
            if category_status == "proposed_addition":
                proposals.append(
                    {
                        "proposal_id": digest([org_id, resolved_id, "category"]),
                        "task_id": resolved_id,
                        "field": "category",
                        "previous_value": existing,
                        "proposed_value": categories[0],
                        "evidence_ids": sorted(category_evidence[categories[0]]),
                    }
                )
        for row in rows.values():
            if row["kind"] != "delivery_membership":
                continue
            facts = row["facts"]
            observations.append(
                {
                    "observation_id": digest([org_id, row["record_id"]]),
                    "profile_id": profile_id,
                    "task_id": resolved_id,
                    "identity_status": resolution,
                    "customer_label": facts["customer"],
                    "program": None,
                    "batch": facts["batch"],
                    "date_from_source": facts["delivery_date_from_source"],
                    "customer_task_name": facts["delivery_name"],
                    "membership": facts["membership"],
                    "shipped_version_id": None,
                    "shipped_content_hash": None,
                    "customer_acceptance": "unknown",
                    "finalized_at": None,
                    "evidence_id": row["record_id"],
                }
            )

    profiles.sort(key=lambda r: r["profile_id"])
    observations.sort(key=lambda r: r["observation_id"])
    proposals.sort(key=lambda r: r["proposal_id"])
    return {
        "schema_version": PLAN_SCHEMA,
        "org_id": org_id,
        "mode": "preview_only",
        "source_as_of": bundle["summary"]["as_of_date"],
        "source_revision": bundle["summary"]["repository_revision"],
        "inventory_captured_at": inventory.get("captured_at") if inventory else None,
        "input_hashes": {
            "bundle": digest(bundle),
            "inventory": digest(inventory) if inventory else None,
        },
        "summary": {
            "source_records": len(evidence),
            "task_profiles": len(profiles),
            "identity_status_counts": dict(
                sorted(Counter(p["identity_status"] for p in profiles).items())
            ),
            "category_status_counts": dict(
                sorted(Counter(p["category_status"] for p in profiles).items())
            ),
            "metadata_proposals": len(proposals),
            "delivery_observations": len(observations),
            "resolved_delivery_observations": sum(
                o["task_id"] is not None for o in observations
            ),
        },
        "task_profiles": profiles,
        "metadata_proposals": proposals,
        "delivery_observations": observations,
        "evidence": evidence,
        "source_discrepancies": bundle.get("discrepancies", []),
        "sources": bundle["sources"],
    }


def compare_plans(previous: dict, current: dict) -> dict:
    """Missing source rows are review items, never instructions to erase history."""
    if (
        previous.get("schema_version") != PLAN_SCHEMA
        or previous.get("org_id") != current["org_id"]
    ):
        raise ValueError("previous plan must have the same schema and organization")
    before = {r["record_id"]: r["content_hash"] for r in previous["evidence"]}
    after = {r["record_id"]: r["content_hash"] for r in current["evidence"]}
    return {
        "added": sorted(after.keys() - before.keys()),
        "changed": sorted(
            k for k in after.keys() & before.keys() if after[k] != before[k]
        ),
        "unchanged": sorted(
            k for k in after.keys() & before.keys() if after[k] == before[k]
        ),
        "missing_from_new_source_requires_review": sorted(before.keys() - after.keys()),
    }


def render_report(plan: dict) -> str:
    counts = plan["summary"]
    lines = [
        "# Delivery metadata backfill preview",
        "",
        f"Source snapshot: {plan['source_as_of']}. Organization: `{plan['org_id']}`.",
        "",
        "This is a local preview. No Oddish records or shipment states were changed.",
        "",
        f"- {counts['source_records']:,} source records retained.",
        f"- {counts['task_profiles']:,} groups of names linked by explicit source evidence.",
        f"- {counts['delivery_observations']:,} historical/current batch observations, not unique shipments.",
        f"- {counts['resolved_delivery_observations']:,} observations matched to an inventory task ID.",
        f"- {counts['metadata_proposals']:,} proposed metadata additions.",
        "",
        "## Identity matching",
        "",
        "| Result | Task groups |",
        "| --- | ---: |",
    ]
    lines += [f"| {k} | {v:,} |" for k, v in counts["identity_status_counts"].items()]
    lines += [
        "",
        "An explicit source ID is confirmed only when it exists in the supplied",
        "organization inventory without a conflicting name or ID. Name-only matches",
        "require review. No inventory means no resolved IDs or metadata proposals.",
        "",
        "## Category evidence",
        "",
        "| Result | Task groups |",
        "| --- | ---: |",
    ]
    lines += [f"| {k} | {v:,} |" for k, v in counts["category_status_counts"].items()]
    lines += [
        "",
        "Conflicting values are preserved; the planner does not choose a winner.",
        "",
        "## Review next",
        "",
        "1. Supply a fresh organization inventory if none was supplied.",
        "2. Review conflicting IDs, names, and category values in task_profiles.",
        "3. Inspect metadata_proposals and their evidence_ids before implementing application.",
        "4. Retain unknown shipped versions, program, acceptance, and finalization.",
        "",
        "Pass-rate version lists and repair-tracker current versions are not shipment pins.",
        "All history coverage remains partial; absence never establishes that a lab has",
        "never received a task. Verbatim source rows are in evidence-raw.json, not the plan.",
        "",
    ]
    conflicts = [
        p
        for p in plan["task_profiles"]
        if p["identity_status"] in {"conflicting_explicit_ids", "name_collision"}
    ]
    if conflicts:
        lines += [
            "## Identity conflicts",
            "",
            "All conflicts and their source rows are in `identity-conflicts.json`.",
            "Examples (names and IDs are quoted from the inputs):",
            "",
        ]
        for profile in conflicts[:5]:
            lines.append(
                f"- {', '.join(profile['names'])}: "
                f"{', '.join(profile['explicit_task_ids'])}"
            )
        lines.append("")
    if "comparison" in plan:
        lines += ["## Previous preview comparison", ""]
        lines += [f"- {k}: {len(v):,}" for k, v in plan["comparison"].items()]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--org-id", required=True)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--previous-plan", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        bundle_bytes = args.bundle.read_bytes()
        inventory_bytes = args.inventory.read_bytes() if args.inventory else None
        raw_rows: dict[str, dict] = {}
        plan = build_plan(
            json.loads(bundle_bytes),
            org_id=args.org_id,
            inventory=json.loads(inventory_bytes)
            if inventory_bytes is not None
            else None,
            raw_sink=raw_rows,
        )
        plan["input_file_sha256"] = {
            "bundle": hashlib.sha256(bundle_bytes).hexdigest(),
            "inventory": hashlib.sha256(inventory_bytes).hexdigest()
            if inventory_bytes is not None
            else None,
        }
        if args.previous_plan:
            plan["comparison"] = compare_plans(
                json.loads(args.previous_plan.read_bytes()), plan
            )
        # Exclusive directory creation protects earlier evidence and all inputs.
        args.output_dir.mkdir(parents=True, exist_ok=False)
        # Compact: the plan is uploaded, not read by people; README.md and
        # identity-conflicts.json are the readable outputs.
        (args.output_dir / "plan.json").write_text(
            json.dumps(plan, ensure_ascii=False, separators=(",", ":")) + "\n"
        )
        (args.output_dir / "README.md").write_text(render_report(plan))
        # Verbatim rows stay local: they carry names and notes the import
        # never needs, and they must not reach the server.
        (args.output_dir / "evidence-raw.json").write_text(
            json.dumps(raw_rows, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        )
        evidence = {r["record_id"]: r for r in plan["evidence"]}
        conflicts = [
            {**p, "identity_evidence": [evidence[e] for e in p["evidence_ids"]]}
            for p in plan["task_profiles"]
            if p["identity_status"] in {"conflicting_explicit_ids", "name_collision"}
        ]
        (args.output_dir / "identity-conflicts.json").write_text(
            json.dumps(conflicts, indent=2, ensure_ascii=False) + "\n"
        )
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"backfill preview failed: {exc}\n")
    print(json.dumps(plan["summary"], indent=2))


if __name__ == "__main__":
    main()
