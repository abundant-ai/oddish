"""Preview or apply a reviewed delivery metadata plan to Oddish.

Served by ``POST /deliveries/history-imports``. Takes the planner's plan and
the inventory it was built from, checks the organization and live task
identities, and computes the exact rows the plan would create or update:
retained source records, task aliases, metadata assertions, and delivery
history for profiles with a resolved task ID. A preview stops there and
records the counts; an apply executes them in the caller's transaction.
Everything unresolved is counted in the receipt, never invented.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from typing import Any

from oddish.core.ingest.delivery_backfill import (
    PLAN_SCHEMA,
    digest,
    validate_inventory,
)
from oddish.core.ingest.delivery_inventory import org_clause, org_label

# Rejection details are bounded so receipts stay readable; the count is kept.
PROBLEM_LIMIT = 50
CHUNK = 1000
# The live identity check is the real freshness guard; the age limit only
# stops a forgotten inventory from being reused months later.
DEFAULT_MAX_INVENTORY_AGE = timedelta(days=7)
# An apply must follow a preview of the same plan this recently.
PREVIEW_WINDOW = timedelta(hours=24)
IMPORT_LOCK_KEY = "delivery-metadata-import"


class PlanRejected(Exception):
    """The plan must not be applied; ``details`` lists each problem."""

    def __init__(self, reason: str, details: list[str] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.details = details or []


def _chunks(items: list, size: int = CHUNK):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _describe(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    count, unit = (hours // 24, "day") if hours % 24 == 0 else (hours, "hour")
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def load_json_document(data: bytes, label: str) -> dict:
    """A JSON object from uploaded bytes; anything else is a ValueError."""
    try:
        document = json.loads(data)
    except ValueError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{label} must be a JSON object")
    return document


def parse_plan(data: bytes) -> tuple[dict, str]:
    """The plan document and its content hash. CPU-bound; callers on an
    event loop run it in a thread."""
    plan = load_json_document(data, "plan")
    return plan, digest(plan)


def parse_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_customer_map(items: list[str]) -> dict[str, str]:
    """``label=customer`` pairs: a source customer label to a customers row
    (id or name). Labels without a pair keep ``customer_id`` NULL."""
    mapping: dict[str, str] = {}
    for item in items:
        label, separator, ref = item.partition("=")
        if not separator or not label.strip() or not ref.strip():
            raise ValueError(f"customer map entry must be label=customer: {item!r}")
        if label.strip() in mapping:
            raise ValueError(f"customer label mapped twice: {label.strip()!r}")
        mapping[label.strip()] = ref.strip()
    return mapping


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_plan_inputs(
    plan: dict,
    inventory: dict | None,
    org_label_value: str,
    *,
    now: datetime,
    max_inventory_age: timedelta,
) -> tuple[dict[str, dict], datetime]:
    """Static checks before any database read. Returns inventory tasks by ID
    and the inventory capture time."""
    if plan.get("schema_version") != PLAN_SCHEMA:
        raise PlanRejected(f"plan schema must be {PLAN_SCHEMA}")
    if plan.get("org_id") != org_label_value:
        raise PlanRejected(
            "plan organization does not match the requested organization"
        )
    if plan.get("mode") != "preview_only":
        raise PlanRejected("plan must be a planner preview")
    expected_hash = (plan.get("input_hashes") or {}).get("inventory")
    if inventory is None or not expected_hash:
        raise PlanRejected(
            "plan was built without an organization inventory; nothing is resolvable"
        )
    if digest(inventory) != expected_hash:
        raise PlanRejected("inventory is not the one the plan was built from")
    try:
        tasks = validate_inventory(inventory, org_label_value)
    except (ValueError, KeyError, TypeError) as exc:
        raise PlanRejected(f"inventory is invalid: {exc}") from exc
    try:
        captured = parse_timestamp(
            inventory.get("captured_at"), "inventory captured_at"
        )
    except ValueError as exc:
        raise PlanRejected(str(exc)) from exc
    if captured > now + timedelta(minutes=5):
        raise PlanRejected("inventory captured_at is in the future")
    if now - captured > max_inventory_age:
        raise PlanRejected(
            f"inventory captured at {captured.isoformat()} is older than "
            f"{_describe(max_inventory_age)}; export a fresh inventory and re-plan"
        )
    return tasks, captured


async def _lock_imports(session, label: str) -> None:
    """Serialize imports per organization for the rest of the transaction, so
    concurrent applies become a replay instead of a constraint race."""
    from sqlalchemy import text

    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"{IMPORT_LOCK_KEY}:{label}"},
    )


async def _require_recent_preview(session, label: str, plan_hash: str, now) -> None:
    from sqlalchemy import select
    from oddish.db.models import MetadataImportReceiptModel

    previewed = await session.scalar(
        select(MetadataImportReceiptModel.id).where(
            MetadataImportReceiptModel.org_id == label,
            MetadataImportReceiptModel.plan_hash == plan_hash,
            MetadataImportReceiptModel.outcome == "previewed",
            MetadataImportReceiptModel.created_at >= now - PREVIEW_WINDOW,
        )
    )
    if previewed is None:
        raise PlanRejected(
            "apply requires a preview of this exact plan within the last "
            f"{_describe(PREVIEW_WINDOW)}; preview it, review the receipt, then apply"
        )


async def _resolve_customers(session, org_id: str | None, customer_map: dict[str, str]):
    from sqlalchemy import or_, select
    from oddish.db.models import CustomerModel

    resolved: dict[str, str] = {}
    for label, ref in sorted(customer_map.items()):
        row = await session.scalar(
            select(CustomerModel).where(
                org_clause(CustomerModel.org_id, org_id),
                CustomerModel.deleted_at.is_(None),
                or_(CustomerModel.id == ref, CustomerModel.name == ref),
            )
        )
        if row is None:
            raise PlanRejected(
                f"customer {ref!r} for source label {label!r} does not exist in "
                f"organization {org_label(org_id)}"
            )
        resolved[label] = row.id
    return resolved


def _proposed_aliases(plan: dict) -> dict[str, tuple[str, list[str]]]:
    """name -> (task_id, evidence_ids) from ``source_names`` proposals."""
    wanted: dict[str, tuple[str, list[str]]] = {}
    for proposal in plan["metadata_proposals"]:
        if proposal["field"] == "source_names":
            for name in proposal["proposed_value"]:
                wanted[name] = (proposal["task_id"], proposal["evidence_ids"])
    return wanted


def _live_alias_clause(model):
    return model.valid_until.is_(None), model.retracted_at.is_(None)


async def _live_identity_problems(
    session, plan: dict, inventory_tasks: dict[str, dict], org_id: str | None
) -> list[str]:
    """Identity drift since the inventory. Any problem rejects the plan: the
    review was made against identities that no longer hold."""
    from sqlalchemy import select
    from oddish.db.models import TaskAliasModel, TaskModel

    label = org_label(org_id)
    problems: list[str] = []
    resolved_ids = sorted({p["task_id"] for p in plan["task_profiles"] if p["task_id"]})
    live: dict[str, tuple[str, str | None]] = {}
    for chunk in _chunks(resolved_ids):
        rows = await session.execute(
            select(TaskModel.id, TaskModel.name, TaskModel.org_id)
            .where(TaskModel.id.in_(chunk))
            .execution_options(include_deleted=True)
        )
        live.update({row.id: (row.name, row.org_id) for row in rows.all()})
    for task_id in resolved_ids:
        row = live.get(task_id)
        if row is None or row[1] != org_id:
            problems.append(f"task {task_id} is not in organization {label}")
        elif row[0] != inventory_tasks[task_id]["name"]:
            problems.append(
                f"task {task_id} is now named {row[0]!r}; the inventory said "
                f"{inventory_tasks[task_id]['name']!r}"
            )

    wanted = _proposed_aliases(plan)
    for chunk in _chunks(sorted(wanted)):
        rows = await session.execute(
            select(TaskModel.id, TaskModel.name).where(
                org_clause(TaskModel.org_id, org_id), TaskModel.name.in_(chunk)
            )
        )
        for row in rows.all():
            if row.id != wanted[row.name][0]:
                problems.append(
                    f"alias {row.name!r} for task {wanted[row.name][0]} is the "
                    f"current name of task {row.id}"
                )
        rows = await session.execute(
            select(TaskAliasModel.name, TaskAliasModel.task_id).where(
                TaskAliasModel.org_id == label,
                TaskAliasModel.name.in_(chunk),
                *_live_alias_clause(TaskAliasModel),
            )
        )
        for row in rows.all():
            if row.task_id != wanted[row.name][0]:
                problems.append(
                    f"alias {row.name!r} for task {wanted[row.name][0]} already "
                    f"belongs to task {row.task_id}"
                )
    return problems


# ---------------------------------------------------------------------------
# Diffs: what the plan would change, computed from reads only
# ---------------------------------------------------------------------------


@dataclass
class TableDiff:
    """Rows one table would gain or change. Every dict in ``update`` and
    ``touch`` carries the primary key; ``touch`` only refreshes bookkeeping."""

    create: list[dict] = field(default_factory=list)
    update: list[dict] = field(default_factory=list)
    touch: list[dict] = field(default_factory=list)
    unchanged: int = 0
    notes: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, Any]:
        return {
            "created": len(self.create),
            "updated": len(self.update),
            "unchanged": self.unchanged,
            **self.notes,
        }


@dataclass
class ImportChanges:
    source_records: TableDiff
    aliases: TableDiff
    assertions: TableDiff
    history: TableDiff

    def summary(self) -> dict[str, dict]:
        return {
            "source_records": self.source_records.counts(),
            "aliases": self.aliases.counts(),
            "assertions": self.assertions.counts(),
            "delivery_history": self.history.counts(),
        }


async def _diff_source_records(session, plan, label, receipt_id, now) -> TableDiff:
    from sqlalchemy import select
    from oddish.db.models import TaskSourceRecordModel

    existing: dict[str, str] = {}
    for chunk in _chunks([r["record_id"] for r in plan["evidence"]]):
        rows = await session.execute(
            select(
                TaskSourceRecordModel.record_id, TaskSourceRecordModel.content_hash
            ).where(
                TaskSourceRecordModel.org_id == label,
                TaskSourceRecordModel.record_id.in_(chunk),
            )
        )
        existing.update(dict(rows.all()))
    diff = TableDiff()
    for record in plan["evidence"]:
        key = {"org_id": label, "record_id": record["record_id"]}
        values = {
            "kind": record["kind"],
            "source_key": record["source_key"],
            "names": record["names"],
            "explicit_task_ids": record["explicit_task_ids"],
            "source_urls": record["source_urls"],
            "facts": record["facts"],
            "content_hash": record["content_hash"],
            "last_import_id": receipt_id,
        }
        known = existing.get(record["record_id"])
        if known is None:
            diff.create.append({**key, **values, "first_import_id": receipt_id})
        elif known != record["content_hash"]:
            diff.update.append({**key, **values, "updated_at": now})
        else:
            # Still present in the source: the receipt that last saw it.
            diff.touch.append({**key, "last_import_id": receipt_id})
            diff.unchanged += 1
    return diff


async def _diff_aliases(session, plan, label, receipt_id, now) -> TableDiff:
    from sqlalchemy import select
    from oddish.db.models import TaskAliasModel, generate_id

    wanted = _proposed_aliases(plan)
    diff = TableDiff()
    for chunk in _chunks(sorted(wanted)):
        rows = (
            await session.scalars(
                select(TaskAliasModel).where(
                    TaskAliasModel.org_id == label,
                    TaskAliasModel.name.in_(chunk),
                    *_live_alias_clause(TaskAliasModel),
                )
            )
        ).all()
        by_name = {row.name: row for row in rows}
        for name in chunk:
            task_id, evidence_ids = wanted[name]
            row = by_name.get(name)
            if row is None:
                diff.create.append(
                    {
                        "id": generate_id(),
                        "org_id": label,
                        "task_id": task_id,
                        "name": name,
                        "evidence_ids": evidence_ids,
                        "import_id": receipt_id,
                    }
                )
            elif row.evidence_ids != evidence_ids:
                # Identity drift was rejected earlier, so the task matches.
                diff.update.append(
                    {
                        "id": row.id,
                        "evidence_ids": evidence_ids,
                        "import_id": receipt_id,
                        "updated_at": now,
                    }
                )
            else:
                diff.unchanged += 1
    return diff


async def _diff_assertions(session, plan, label, receipt_id, now) -> TableDiff:
    from sqlalchemy import select
    from oddish.db.models import TaskMetadataAssertionModel, generate_id

    # Only proposals are facts. A resolved profile with conflicting category
    # values is exposed in the summary, not imported.
    proposals = [p for p in plan["metadata_proposals"] if p["field"] != "source_names"]
    diff = TableDiff()
    for chunk in _chunks(proposals):
        rows = (
            await session.scalars(
                select(TaskMetadataAssertionModel).where(
                    TaskMetadataAssertionModel.org_id == label,
                    TaskMetadataAssertionModel.task_id.in_(
                        sorted({p["task_id"] for p in chunk})
                    ),
                )
            )
        ).all()
        existing = {(row.task_id, row.field, row.value): row for row in rows}
        for proposal in chunk:
            key = (proposal["task_id"], proposal["field"], proposal["proposed_value"])
            row = existing.get(key)
            if row is None:
                diff.create.append(
                    {
                        "id": generate_id(),
                        "org_id": label,
                        "task_id": key[0],
                        "field": key[1],
                        "value": key[2],
                        "evidence_ids": proposal["evidence_ids"],
                        "import_id": receipt_id,
                    }
                )
            elif row.evidence_ids != proposal["evidence_ids"]:
                diff.update.append(
                    {
                        "id": row.id,
                        "evidence_ids": proposal["evidence_ids"],
                        "import_id": receipt_id,
                        "updated_at": now,
                    }
                )
            else:
                diff.unchanged += 1
    return diff


# History columns a source row always states.
_HISTORY_SOURCE_FIELDS = {
    "customer_label": "customer_label",
    "batch": "batch",
    "source_date": "date_from_source",
    "customer_task_name": "customer_task_name",
    "membership": "membership",
}
# History columns a source establishes only when it has a value. They never
# reset a value recorded earlier (an operator-confirmed customer, a shipped
# version, a customer decision).
_HISTORY_ESTABLISHED_FIELDS = {
    "program": "program",
    "shipped_version_id": "shipped_version_id",
    "shipped_content_hash": "shipped_content_hash",
    "finalized_at": "finalized_at",
}


async def _diff_history(
    session, plan, label, receipt_id, now, customers: dict[str, str]
) -> TableDiff:
    from sqlalchemy import select
    from oddish.db.models import TaskDeliveryHistoryModel

    observations = [o for o in plan["delivery_observations"] if o["task_id"]]
    diff = TableDiff(
        notes={
            "skipped_unresolved": len(plan["delivery_observations"])
            - len(observations),
            "customer_labels_unmapped": sorted(
                {
                    o["customer_label"]
                    for o in observations
                    if o["customer_label"] not in customers
                }
            ),
        }
    )
    for chunk in _chunks(observations):
        rows = (
            await session.scalars(
                select(TaskDeliveryHistoryModel).where(
                    TaskDeliveryHistoryModel.id.in_(
                        [o["observation_id"] for o in chunk]
                    )
                )
            )
        ).all()
        existing = {row.id: row for row in rows}
        for observation in chunk:
            stated = {
                column: observation[key]
                for column, key in _HISTORY_SOURCE_FIELDS.items()
            }
            established = {
                column: observation[key]
                for column, key in _HISTORY_ESTABLISHED_FIELDS.items()
                if observation[key] is not None
            }
            if observation["customer_acceptance"] != "unknown":
                established["customer_acceptance"] = observation["customer_acceptance"]
            customer_id = customers.get(observation["customer_label"])
            if customer_id is not None:
                established["customer_id"] = customer_id
            row = existing.get(observation["observation_id"])
            if row is None:
                diff.create.append(
                    {
                        "id": observation["observation_id"],
                        "org_id": label,
                        "task_id": observation["task_id"],
                        "source_record_id": observation["evidence_id"],
                        "import_id": receipt_id,
                        **stated,
                        **established,
                    }
                )
                continue
            if row.org_id != label or row.task_id != observation["task_id"]:
                raise PlanRejected(
                    "history conflicts with an earlier import",
                    [
                        f"observation {observation['observation_id']} is recorded "
                        f"for task {row.task_id}, plan says {observation['task_id']}"
                    ],
                )
            wanted = {**stated, **established}
            if all(getattr(row, column) == value for column, value in wanted.items()):
                diff.unchanged += 1
                continue
            # Uniform keys per row keep the bulk update to one statement.
            current = {
                column: getattr(row, column)
                for column in (*_HISTORY_SOURCE_FIELDS, *_HISTORY_ESTABLISHED_FIELDS)
            }
            current.update(
                customer_acceptance=row.customer_acceptance, customer_id=row.customer_id
            )
            diff.update.append(
                {
                    "id": row.id,
                    **current,
                    **wanted,
                    "import_id": receipt_id,
                    "updated_at": now,
                }
            )
    return diff


async def diff_plan(session, plan, label, receipt_id, now, customers) -> ImportChanges:
    return ImportChanges(
        source_records=await _diff_source_records(
            session, plan, label, receipt_id, now
        ),
        aliases=await _diff_aliases(session, plan, label, receipt_id, now),
        assertions=await _diff_assertions(session, plan, label, receipt_id, now),
        history=await _diff_history(session, plan, label, receipt_id, now, customers),
    )


async def execute_changes(session, changes: ImportChanges) -> None:
    """Bulk-write the diffs, in foreign-key order, in the caller's transaction."""
    from sqlalchemy import insert, update
    from oddish.db.models import (
        TaskAliasModel,
        TaskDeliveryHistoryModel,
        TaskMetadataAssertionModel,
        TaskSourceRecordModel,
    )

    for diff, model in (
        (changes.source_records, TaskSourceRecordModel),
        (changes.aliases, TaskAliasModel),
        (changes.assertions, TaskMetadataAssertionModel),
        (changes.history, TaskDeliveryHistoryModel),
    ):
        for rows in _chunks(diff.create):
            await session.execute(insert(model), rows)
        for rows in _chunks(diff.update):
            await session.execute(update(model), rows)
        for rows in _chunks(diff.touch):
            await session.execute(update(model), rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def apply_plan_core(
    session,
    *,
    plan: dict,
    inventory: dict | None,
    org_id: str | None,
    mode: str = "preview",
    plan_hash: str | None = None,
    customer_map: dict[str, str] | None = None,
    user_id: str | None = None,
    now: datetime | None = None,
    max_inventory_age: timedelta = DEFAULT_MAX_INVENTORY_AGE,
):
    """Record an import receipt and, in ``apply`` mode, the plan's facts.

    ``preview`` computes the diff from reads only and records it, so its
    receipt reports exactly what ``apply`` would do without writing a row.
    ``apply`` requires a recent preview of the same plan, serializes with
    other applies in the organization, and executes the diff. A rejected
    plan gets a receipt with the reasons and nothing else. The caller owns
    the transaction. ``org_id`` None is the standalone server; its documents
    and history rows carry the label ``local``.
    """
    from oddish.db.models import MetadataImportReceiptModel, utcnow

    if mode not in {"preview", "apply"}:
        raise ValueError("mode must be preview or apply")
    label = org_label(org_id)
    now = now or utcnow()
    plan_hash = plan_hash or digest(plan)
    receipt = MetadataImportReceiptModel(
        org_id=label,
        plan_schema=str(plan.get("schema_version")),
        plan_hash=plan_hash,
        mode=mode,
        outcome="rejected",
        source_as_of=plan.get("source_as_of"),
        source_revision=plan.get("source_revision"),
        input_hashes=plan.get("input_hashes") or {},
        created_by_user_id=user_id,
        created_at=now,
    )
    session.add(receipt)
    try:
        inventory_tasks, captured = check_plan_inputs(
            plan, inventory, label, now=now, max_inventory_age=max_inventory_age
        )
        receipt.inventory_captured_at = captured
        if mode == "apply":
            await _lock_imports(session, label)
            await _require_recent_preview(session, label, plan_hash, now)
        customers = await _resolve_customers(session, org_id, customer_map or {})
        problems = await _live_identity_problems(session, plan, inventory_tasks, org_id)
        if problems:
            raise PlanRejected(
                "task identities changed since the inventory; re-plan", problems
            )
        await session.flush()
        changes = await diff_plan(session, plan, label, receipt.id, now, customers)
        if mode == "apply":
            await execute_changes(session, changes)
    except PlanRejected as exc:
        receipt.rejection_reason = exc.reason
        receipt.summary = {
            "problems": exc.details[:PROBLEM_LIMIT],
            "problem_count": len(exc.details),
        }
        await session.flush()
        return receipt
    profiles = plan["task_profiles"]
    receipt.outcome = "applied" if mode == "apply" else "previewed"
    receipt.summary = {
        "identity_status_counts": plan["summary"]["identity_status_counts"],
        "resolved_profiles": sum(1 for p in profiles if p["task_id"]),
        "unresolved_profiles": sum(1 for p in profiles if not p["task_id"]),
        "category_conflicts_unresolved": sum(
            1 for p in profiles if p["task_id"] and p["category_status"] == "conflict"
        ),
        **changes.summary(),
    }
    await session.flush()
    return receipt


async def list_import_receipts_core(session, *, org_id: str | None, limit: int = 50):
    """Newest import receipts for one organization."""
    from sqlalchemy import select
    from oddish.db.models import MetadataImportReceiptModel

    return (
        await session.scalars(
            select(MetadataImportReceiptModel)
            .where(MetadataImportReceiptModel.org_id == org_label(org_id))
            .order_by(MetadataImportReceiptModel.created_at.desc())
            .limit(limit)
        )
    ).all()
