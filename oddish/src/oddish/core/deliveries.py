"""Delivery checklists: is this set of tasks good to ship? (docs/delivery-design.md)

Readiness is derived at read time, never stored. Every automated check
evaluates the task's *current default version* (``tasks.current_version_id``),
so publishing a new version resets the board on its own. Manual ticks record
the version they attested to and only count while it is still the default.
"""

from __future__ import annotations

import hashlib
from typing import Any, Sequence

from fastapi import HTTPException
from sqlalchemy import case, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import (
    DeliveryManualCheckModel,
    DeliveryModel,
    DeliverySnapshotModel,
    DeliveryTaskModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    utcnow,
)
from oddish.filters.trial_predicates import EligibleTrialScope
from oddish.schemas import (
    DeliveryBoardResponse,
    DeliveryCheckConfig,
    DeliveryCheckResult,
    DeliveryCreate,
    DeliveryDefect,
    DeliveryListItem,
    DeliveryPatch,
    DeliveryResponse,
    DeliveryTaskBoardRow,
    DeliveryTasksAdd,
    ManualCheckSet,
    TaskQAHistoryResponse,
    TaskQAHistoryRun,
    TaskQAHistoryVersion,
)

# The automated checks a delivery can run, with their default parameters.
# ``check_config["automated"]`` merges over these per key; unknown keys are
# rejected at write time so a typo cannot silently disable a check.
# ``min_rollouts`` defaults mirror the verdict evidence bar
# (``MIN_VERDICT_TRIALS`` / ``MIN_VERDICT_AGENTS`` in analysis_trials).
DEFAULT_AUTOMATED_CHECKS: dict[str, dict[str, Any]] = {
    "pre_trial_passed": {"enabled": True},
    "min_rollouts": {"enabled": True, "min_trials": 5, "min_agents": 3},
    "verdict_ok": {"enabled": True},
    "no_must_fix": {"enabled": True},
}

# Reserved manual-check keys. Every task must carry a 'signoff' tick, and
# each open must-fix defect needs its own 'ack:<defect-id>' tick before a
# person can sign the task off. A failing automated check can be shipped
# anyway with a 'waive:<check-key>' acknowledgement. All three record who
# ticked and which version.
SIGNOFF_CHECK_KEY = "signoff"
ACK_CHECK_PREFIX = "ack:"
WAIVE_CHECK_PREFIX = "waive:"
# 'no_must_fix' is not waivable as a whole: each defect needs its own ack.
WAIVABLE_CHECKS = frozenset(DEFAULT_AUTOMATED_CHECKS) - {"no_must_fix"}

_CHECK_LABELS = {
    "pre_trial_passed": "Pre-trial audit passed",
    "min_rollouts": "Enough rollouts",
    "verdict_ok": "Verdict accepts",
    "no_must_fix": "No must-fix defects",
}


def _normalized_check_config(raw: dict | None) -> DeliveryCheckConfig:
    """Merge stored config over the defaults; reject unknown automated keys."""
    config = DeliveryCheckConfig.model_validate(raw or {})
    unknown = set(config.automated) - set(DEFAULT_AUTOMATED_CHECKS)
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"unknown automated checks: {', '.join(sorted(unknown))}",
        )
    for definition in config.manual:
        if (
            definition.key == SIGNOFF_CHECK_KEY
            or definition.key.startswith(ACK_CHECK_PREFIX)
            or definition.key.startswith(WAIVE_CHECK_PREFIX)
        ):
            raise HTTPException(
                status_code=422,
                detail="'signoff', 'ack:*' and 'waive:*' are reserved check keys",
            )
    merged = {
        key: {**defaults, **config.automated.get(key, {})}
        for key, defaults in DEFAULT_AUTOMATED_CHECKS.items()
    }
    return DeliveryCheckConfig(automated=merged, manual=config.manual)


async def _get_delivery(
    session: AsyncSession,
    delivery_id: str,
    org_id: str | None,
    *,
    for_update: bool = False,
) -> DeliveryModel:
    """Fetch one org-scoped delivery.

    Mutations pass ``for_update=True`` so they serialize on the delivery
    row: without it, a concurrent add could land between finalize's green
    check and its snapshot, finalizing a board that omits the new task.
    """
    delivery = await session.get(
        DeliveryModel, delivery_id, with_for_update=bool(for_update) or None
    )
    if delivery is None or delivery.org_id != org_id:
        raise HTTPException(status_code=404, detail="delivery not found")
    return delivery


def _require_active(delivery: DeliveryModel) -> None:
    if delivery.status != "active":
        raise HTTPException(
            status_code=409, detail="delivery is finalized and read-only"
        )


async def _member_rows(
    session: AsyncSession, delivery_id: str
) -> list[DeliveryTaskModel]:
    return list(
        (
            await session.scalars(
                select(DeliveryTaskModel)
                .where(DeliveryTaskModel.delivery_id == delivery_id)
                .order_by(DeliveryTaskModel.sort_order, DeliveryTaskModel.created_at)
            )
        ).all()
    )


# =============================================================================
# CRUD
# =============================================================================


async def create_delivery_core(
    session: AsyncSession,
    *,
    data: DeliveryCreate,
    org_id: str | None,
    user_id: str | None,
) -> DeliveryModel:
    check_config = (
        _normalized_check_config(data.check_config.model_dump())
        if data.check_config is not None
        else None
    )
    delivery = DeliveryModel(
        org_id=org_id,
        created_by_user_id=user_id,
        name=data.name,
        customer_name=data.customer_name,
        description=data.description,
        check_config=check_config.model_dump() if check_config else {},
    )
    session.add(delivery)
    await session.flush()
    if data.task_ids:
        await _add_tasks(session, delivery, data.task_ids, org_id)
    return delivery


async def list_deliveries_core(
    session: AsyncSession, *, org_id: str | None
) -> list[DeliveryListItem]:
    counts = (
        select(
            DeliveryTaskModel.delivery_id,
            func.count().label("task_count"),
        )
        .group_by(DeliveryTaskModel.delivery_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(DeliveryModel, func.coalesce(counts.c.task_count, 0))
            .outerjoin(counts, counts.c.delivery_id == DeliveryModel.id)
            .where(DeliveryModel.org_id == org_id)
            .order_by(DeliveryModel.created_at.desc())
        )
    ).all()
    return [
        DeliveryListItem(
            **DeliveryResponse.model_validate(delivery).model_dump(),
            task_count=task_count,
        )
        for delivery, task_count in rows
    ]


async def patch_delivery_core(
    session: AsyncSession,
    *,
    delivery_id: str,
    org_id: str | None,
    data: DeliveryPatch,
) -> DeliveryModel:
    delivery = await _get_delivery(session, delivery_id, org_id, for_update=True)
    _require_active(delivery)
    if data.name is not None:
        delivery.name = data.name
    if "customer_name" in data.model_fields_set:
        delivery.customer_name = data.customer_name
    if "description" in data.model_fields_set:
        delivery.description = data.description
    if data.check_config is not None:
        delivery.check_config = _normalized_check_config(
            data.check_config.model_dump()
        ).model_dump()
    await session.flush()
    return delivery


async def delete_delivery_core(
    session: AsyncSession, *, delivery_id: str, org_id: str | None
) -> None:
    delivery = await _get_delivery(session, delivery_id, org_id, for_update=True)
    # A finalized delivery is the permanent record of what shipped; it is
    # read-only like every other mutation path, deletion included.
    _require_active(delivery)
    delivery.deleted_at = utcnow()
    await session.flush()


# =============================================================================
# Membership
# =============================================================================


async def _add_tasks(
    session: AsyncSession,
    delivery: DeliveryModel,
    task_ids: Sequence[str],
    org_id: str | None,
) -> list[DeliveryTaskModel]:
    # Each entry is a task id or a task name — names are unique per org
    # among live tasks (idx_tasks_unique_org_name), so both are unambiguous.
    # An id match wins if a string happens to be both.
    refs = list(dict.fromkeys(task_ids))
    rows = (
        await session.execute(
            select(TaskModel.id, TaskModel.name).where(
                or_(TaskModel.id.in_(refs), TaskModel.name.in_(refs)),
                TaskModel.org_id == org_id,
            )
        )
    ).all()
    known_ids = {task_id for task_id, _ in rows}
    by_name = {name: task_id for task_id, name in rows}
    requested = []
    missing = []
    for ref in refs:
        if ref in known_ids:
            requested.append(ref)
        elif ref in by_name:
            requested.append(by_name[ref])
        else:
            missing.append(ref)
    requested = list(dict.fromkeys(requested))
    if missing:
        raise HTTPException(
            status_code=404, detail=f"tasks not found: {', '.join(missing[:10])}"
        )
    existing = set(
        (
            await session.scalars(
                select(DeliveryTaskModel.task_id).where(
                    DeliveryTaskModel.delivery_id == delivery.id,
                    DeliveryTaskModel.task_id.in_(requested),
                )
            )
        ).all()
    )
    max_order = await session.scalar(
        select(func.coalesce(func.max(DeliveryTaskModel.sort_order), -1)).where(
            DeliveryTaskModel.delivery_id == delivery.id
        )
    )
    next_order = (max_order if max_order is not None else -1) + 1
    added = []
    for task_id in requested:
        if task_id in existing:
            continue
        row = DeliveryTaskModel(
            delivery_id=delivery.id, task_id=task_id, sort_order=next_order
        )
        next_order += 1
        session.add(row)
        added.append(row)
    await session.flush()
    return added


async def add_delivery_tasks_core(
    session: AsyncSession,
    *,
    delivery_id: str,
    org_id: str | None,
    data: DeliveryTasksAdd,
) -> int:
    delivery = await _get_delivery(session, delivery_id, org_id, for_update=True)
    _require_active(delivery)
    added = await _add_tasks(session, delivery, data.task_ids, org_id)
    return len(added)


async def remove_delivery_task_core(
    session: AsyncSession,
    *,
    delivery_id: str,
    org_id: str | None,
    task_id: str,
) -> None:
    delivery = await _get_delivery(session, delivery_id, org_id, for_update=True)
    _require_active(delivery)
    # Accept an id or a name. The id-only lookup comes first so a member
    # whose task was soft-deleted (hidden from TaskModel reads) can still
    # be removed.
    row = await session.scalar(
        select(DeliveryTaskModel).where(
            DeliveryTaskModel.delivery_id == delivery.id,
            DeliveryTaskModel.task_id == task_id,
        )
    )
    if row is None:
        row = await session.scalar(
            select(DeliveryTaskModel)
            .join(TaskModel, TaskModel.id == DeliveryTaskModel.task_id)
            .where(
                DeliveryTaskModel.delivery_id == delivery.id,
                TaskModel.name == task_id,
            )
        )
    if row is None:
        raise HTTPException(status_code=404, detail="task not in this delivery")
    await session.execute(
        delete(DeliveryManualCheckModel).where(
            DeliveryManualCheckModel.delivery_task_id == row.id
        )
    )
    await session.delete(row)
    await session.flush()


def _defect_id(version_id: str, item: dict) -> str:
    """A stable id for one must-fix item, for 'ack:<id>' ticks."""
    raw = item.get("id")
    if isinstance(raw, str) and raw:
        return raw[:56]
    seed = (
        f"{version_id}:{item.get('file')}:{item.get('line_start')}:"
        f"{item.get('title')}"
    )
    return hashlib.sha1(seed.encode()).hexdigest()[:16]


def _pre_trial_items(version: TaskVersionModel) -> list[dict]:
    """The pre-trial audit items, dicts only; any malformed shape reads []."""
    items = (version.pre_trial or {}).get("items")
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


async def _must_fix_items(
    session: AsyncSession, versions: dict[str, TaskVersionModel]
) -> dict[str, list[dict]]:
    """Open must-fix items per version id: ``{id, title, source}``.

    Sources are the version's pre-trial audit and the trial analyses on the
    version. Superseded trials stay in: a defect describes the task version,
    not the run, so retrying the run must not clear the finding (a real fix
    edits the task and lands on a new version anyway).
    """
    out: dict[str, list[dict]] = {vid: [] for vid in versions}
    seen: dict[str, set[str]] = {vid: set() for vid in versions}

    def add(vid: str, item: dict, source: str) -> None:
        defect_id = _defect_id(vid, item)
        if defect_id in seen[vid]:
            return
        seen[vid].add(defect_id)
        out[vid].append(
            {
                "id": defect_id,
                "title": str(item.get("title") or "untitled defect"),
                "source": source,
            }
        )

    for vid, version in versions.items():
        for item in _pre_trial_items(version):
            if item.get("tier") == "must_fix":
                add(vid, item, "pre_trial")

    if versions:
        defect_scope = EligibleTrialScope(
            membership=[TrialModel.task_version_id.in_(list(versions))],
            include_superseded=True,
        )
        # The array-shape guard must live INSIDE the set-returning function:
        # jsonb_array_elements runs in FROM before any WHERE filter, so a row
        # whose action_items is an object or scalar would otherwise raise.
        items = func.jsonb_array_elements(
            case(
                (
                    func.jsonb_typeof(TrialModel.analysis["action_items"])
                    == "array",
                    TrialModel.analysis["action_items"],
                ),
                else_=text("'[]'::jsonb"),
            )
        ).table_valued("value", joins_implicitly=True)
        rows = (
            await session.execute(
                select(TrialModel.task_version_id, items.c.value).where(
                    *defect_scope.clauses(),
                    items.c.value.op("->>")("tier") == "must_fix",
                )
            )
        ).all()
        for vid, item in rows:
            if isinstance(item, dict):
                add(vid, item, "trial")

    return out


# =============================================================================
# Manual checks
# =============================================================================


async def _validate_signoff_or_ack(
    session: AsyncSession,
    delivery: DeliveryModel,
    member: DeliveryTaskModel,
    key: str,
    task_version_id: str | None,
) -> None:
    """Sign-off is the last human tick. Everything red before it needs a
    recorded acknowledgement: each open must-fix defect its own 'ack:',
    each failing automated check a 'waive:'. Acks and waives themselves are
    validated against the current version and the known check keys."""
    if task_version_id is None:
        raise HTTPException(
            status_code=409, detail="task has no default version to attest to"
        )
    version = await session.get(TaskVersionModel, task_version_id)
    if version is None:
        raise HTTPException(
            status_code=409, detail="task has no default version to attest to"
        )
    if key.startswith(WAIVE_CHECK_PREFIX):
        check_key = key[len(WAIVE_CHECK_PREFIX) :]
        if check_key == "no_must_fix":
            raise HTTPException(
                status_code=422,
                detail=(
                    "acknowledge each must-fix defect on its own "
                    "with 'ack:<defect-id>'"
                ),
            )
        if check_key not in WAIVABLE_CHECKS:
            raise HTTPException(
                status_code=404,
                detail=f"'{check_key}' is not an automated check",
            )
        return
    if key.startswith(ACK_CHECK_PREFIX):
        defects = (await _must_fix_items(session, {version.id: version}))[
            version.id
        ]
        if key[len(ACK_CHECK_PREFIX) :] not in {d["id"] for d in defects}:
            raise HTTPException(
                status_code=404,
                detail="defect not found on the task's current version",
            )
        return
    # Sign-off: judge the same board the reader sees, so the rule cannot
    # drift from the display. Unacked defects and unwaived failing checks
    # both refuse it.
    board = await _compute_board(session, delivery)
    row = next(
        (r for r in board.tasks if r.delivery_task_id == member.id), None
    )
    if row is None:
        raise HTTPException(status_code=404, detail="task not in this delivery")
    unacknowledged = [d.id for d in row.defects if not d.acknowledged]
    if unacknowledged:
        raise HTTPException(
            status_code=409,
            detail=(
                "acknowledge the open must-fix defects before sign-off: "
                + ", ".join(unacknowledged[:10])
            ),
        )
    failing = [
        c.key
        for c in row.checks
        if c.kind == "automated" and c.status == "fail" and c.key != "no_must_fix"
    ]
    if failing:
        raise HTTPException(
            status_code=409,
            detail=(
                "acknowledge the failing checks before sign-off "
                "(waive:<check>): " + ", ".join(failing)
            ),
        )


async def set_manual_check_core(
    session: AsyncSession,
    *,
    delivery_id: str,
    org_id: str | None,
    data: ManualCheckSet,
    user_id: str | None,
) -> None:
    delivery = await _get_delivery(session, delivery_id, org_id, for_update=True)
    _require_active(delivery)
    config = _normalized_check_config(delivery.check_config)
    key = data.check_key
    if (
        key == SIGNOFF_CHECK_KEY
        or key.startswith(ACK_CHECK_PREFIX)
        or key.startswith(WAIVE_CHECK_PREFIX)
    ):
        scope_kind = "task"
    else:
        definition = next((m for m in config.manual if m.key == key), None)
        if definition is None:
            raise HTTPException(
                status_code=404, detail=f"manual check '{key}' is not defined"
            )
        scope_kind = definition.scope

    task_version_id: str | None = None
    if scope_kind == "task":
        if data.delivery_task_id is None:
            raise HTTPException(
                status_code=422,
                detail="delivery_task_id is required for a task-scoped check",
            )
        member = await session.scalar(
            select(DeliveryTaskModel).where(
                DeliveryTaskModel.id == data.delivery_task_id,
                DeliveryTaskModel.delivery_id == delivery.id,
            )
        )
        if member is None:
            raise HTTPException(status_code=404, detail="task not in this delivery")
        # The tick attests to the content the human looked at: the task's
        # current default version. A later version change un-ticks it.
        task_version_id = await session.scalar(
            select(TaskModel.current_version_id).where(TaskModel.id == member.task_id)
        )
        if data.checked and (
            key == SIGNOFF_CHECK_KEY
            or key.startswith(ACK_CHECK_PREFIX)
            or key.startswith(WAIVE_CHECK_PREFIX)
        ):
            await _validate_signoff_or_ack(
                session, delivery, member, key, task_version_id
            )
    elif data.delivery_task_id is not None:
        raise HTTPException(
            status_code=422,
            detail="delivery_task_id must be omitted for a delivery-scoped check",
        )

    existing = await session.scalar(
        select(DeliveryManualCheckModel).where(
            DeliveryManualCheckModel.delivery_id == delivery.id,
            DeliveryManualCheckModel.check_key == data.check_key,
            (
                DeliveryManualCheckModel.delivery_task_id == data.delivery_task_id
                if data.delivery_task_id is not None
                else DeliveryManualCheckModel.delivery_task_id.is_(None)
            ),
        )
    )
    if not data.checked:
        if existing is not None:
            await session.delete(existing)
            await session.flush()
        return
    if existing is None:
        existing = DeliveryManualCheckModel(
            delivery_id=delivery.id,
            delivery_task_id=data.delivery_task_id,
            check_key=data.check_key,
        )
        session.add(existing)
    existing.task_version_id = task_version_id
    existing.note = data.note
    existing.checked_by_user_id = user_id
    existing.checked_at = utcnow()
    await session.flush()


# =============================================================================
# Board computation
# =============================================================================


def _check(
    key: str,
    *,
    passed: bool,
    detail: str = "",
    kind: str = "automated",
    label: str | None = None,
    checked_by: str | None = None,
    checked_at: Any = None,
) -> DeliveryCheckResult:
    return DeliveryCheckResult(
        key=key,
        kind=kind,  # type: ignore[arg-type]
        label=label or _CHECK_LABELS.get(key, key),
        status="pass" if passed else "fail",
        detail=detail,
        checked_by_user_id=checked_by,
        checked_at=checked_at,
    )


async def _compute_board(
    session: AsyncSession, delivery: DeliveryModel
) -> DeliveryBoardResponse:
    config = _normalized_check_config(delivery.check_config)
    auto = config.automated
    members = await _member_rows(session, delivery.id)
    task_ids = [m.task_id for m in members]

    tasks: dict[str, TaskModel] = {}
    versions: dict[str, TaskVersionModel] = {}
    max_versions: dict[str, int] = {}
    rollouts: dict[str, tuple[int, int]] = {}
    must_fix_items: dict[str, list[dict]] = {}
    latest_qa_version: dict[str, str] = {}

    if task_ids:
        # Soft-deleted members must stay on the board as failing rows, not
        # vanish: a delivery that silently drops a task could read ready and
        # finalize a snapshot missing tasks still on it.
        tasks = {
            t.id: t
            for t in (
                await session.scalars(
                    select(TaskModel)
                    .where(TaskModel.id.in_(task_ids))
                    .execution_options(include_deleted=True)
                )
            ).all()
        }
        version_ids = [
            t.current_version_id
            for t in tasks.values()
            if t.current_version_id and t.deleted_at is None
        ]
        if version_ids:
            versions = {
                v.id: v
                for v in (
                    await session.scalars(
                        select(TaskVersionModel).where(
                            TaskVersionModel.id.in_(version_ids)
                        )
                    )
                ).all()
            }
            scope = EligibleTrialScope(
                membership=[TrialModel.task_version_id.in_(version_ids)]
            )
            for version_id, count, agents in (
                await session.execute(
                    select(
                        TrialModel.task_version_id,
                        func.count(),
                        func.count(func.distinct(TrialModel.agent)),
                    )
                    .where(*scope.clauses(), TrialModel.status == TrialStatus.SUCCESS)
                    .group_by(TrialModel.task_version_id)
                )
            ).all():
                rollouts[version_id] = (count, agents)

            must_fix_items = await _must_fix_items(session, versions)

        # ``tasks.verdict`` is last-write-wins across versions, so the
        # stored verdict belongs to the NEWEST successful QA run. It only
        # covers the current default when that run graded it. Ordering falls
        # back to created_at so a null finished_at cannot scramble recency.
        qa_rows = (
            await session.execute(
                select(TrialModel.task_id, TrialModel.task_version_id)
                .where(
                    TrialModel.task_id.in_(task_ids),
                    TrialModel.kind == "qa",
                    TrialModel.status == TrialStatus.SUCCESS,
                    TrialModel.task_version_id.isnot(None),
                    # A run staged below the verdict evidence bar carries
                    # with_verdict=false: it completes SUCCESS but restores
                    # the prior verdict instead of authoring one, so it
                    # cannot vouch for the version it graded.
                    func.coalesce(
                        TrialModel.harbor_config["analysis_payload"].op(
                            "->>"
                        )("with_verdict"),
                        "true",
                    )
                    != "false",
                )
                .order_by(
                    func.coalesce(
                        TrialModel.finished_at, TrialModel.created_at
                    ).desc()
                )
            )
        ).all()
        for qa_task_id, qa_version_id in qa_rows:
            latest_qa_version.setdefault(qa_task_id, qa_version_id)

        for task_id, highest in (
            await session.execute(
                select(TaskVersionModel.task_id, func.max(TaskVersionModel.version))
                .where(TaskVersionModel.task_id.in_(task_ids))
                .group_by(TaskVersionModel.task_id)
            )
        ).all():
            max_versions[task_id] = highest

    ticks = (
        await session.scalars(
            select(DeliveryManualCheckModel).where(
                DeliveryManualCheckModel.delivery_id == delivery.id
            )
        )
    ).all()
    task_ticks = {
        (t.delivery_task_id, t.check_key): t for t in ticks if t.delivery_task_id
    }
    delivery_ticks = {t.check_key: t for t in ticks if t.delivery_task_id is None}

    rows: list[DeliveryTaskBoardRow] = []
    for member in members:
        task = tasks.get(member.task_id)
        if task is None or task.deleted_at is not None:
            rows.append(
                DeliveryTaskBoardRow(
                    delivery_task_id=member.id,
                    task_id=member.task_id,
                    task_name=task.name if task else member.task_id,
                    version_id=None,
                    version=None,
                    pinned_version_id=member.pinned_version_id,
                    newer_version_exists=False,
                    is_visible=member.is_visible,
                    sort_order=member.sort_order,
                    customer_note=member.customer_note,
                    internal_note=member.internal_note,
                    checks=[
                        _check(
                            "task_exists",
                            passed=False,
                            detail=(
                                "task was deleted; remove it from this delivery"
                            ),
                            label="Task exists",
                        )
                    ],
                    defects=[],
                    ready=False,
                )
            )
            continue
        version = versions.get(task.current_version_id or "")
        checks: list[DeliveryCheckResult] = []

        def automated(key: str, passed: bool, detail: str) -> None:
            if not auto[key].get("enabled", True):
                checks.append(
                    DeliveryCheckResult(
                        key=key,
                        kind="automated",
                        label=_CHECK_LABELS[key],
                        status="off",
                    )
                )
                return
            if not passed and version is not None:
                # A person may ship a red check anyway, but the override is
                # recorded and bound to the version they looked at.
                waive = task_ticks.get((member.id, WAIVE_CHECK_PREFIX + key))
                if waive is not None and waive.task_version_id == version.id:
                    checks.append(
                        DeliveryCheckResult(
                            key=key,
                            kind="automated",
                            label=_CHECK_LABELS[key],
                            status="waived",
                            detail=detail,
                            checked_by_user_id=waive.checked_by_user_id,
                            checked_at=waive.checked_at,
                        )
                    )
                    return
            checks.append(_check(key, passed=passed, detail=detail))

        defects: list[DeliveryDefect] = []
        if version is None:
            for key in DEFAULT_AUTOMATED_CHECKS:
                automated(key, False, "task has no default version")
        else:
            vlabel = f"v{version.version}"
            for item in must_fix_items.get(version.id, []):
                ack = task_ticks.get((member.id, ACK_CHECK_PREFIX + item["id"]))
                acknowledged = (
                    ack is not None and ack.task_version_id == version.id
                )
                defects.append(
                    DeliveryDefect(
                        id=item["id"],
                        title=item["title"],
                        source=item["source"],
                        acknowledged=acknowledged,
                        acknowledged_by_user_id=(
                            ack.checked_by_user_id if acknowledged else None
                        ),
                        acknowledged_at=ack.checked_at if acknowledged else None,
                    )
                )

            audited = version.pre_trial_status == VerdictStatus.SUCCESS
            automated(
                "pre_trial_passed",
                audited,
                f"audit passed on {vlabel}"
                if audited
                else f"no successful audit on {vlabel}",
            )

            count, agents = rollouts.get(version.id, (0, 0))
            min_trials = int(auto["min_rollouts"].get("min_trials", 5))
            min_agents = int(auto["min_rollouts"].get("min_agents", 3))
            automated(
                "min_rollouts",
                count >= min_trials and agents >= min_agents,
                f"{count}/{min_trials} trials, {agents}/{min_agents} agents "
                f"on {vlabel}",
            )

            verdict = task.verdict if isinstance(task.verdict, dict) else None
            if verdict is None:
                automated("verdict_ok", False, "no verdict yet")
            elif latest_qa_version.get(task.id) != version.id:
                automated(
                    "verdict_ok",
                    False,
                    f"verdict does not cover {vlabel}; re-run QA on it",
                )
            else:
                accepted = bool(verdict.get("is_good"))
                automated(
                    "verdict_ok",
                    accepted,
                    "verdict accepts"
                    if accepted
                    else f"verdict rejects: {verdict.get('primary_issue') or ''}",
                )

            unacknowledged = sum(1 for d in defects if not d.acknowledged)
            if not defects:
                must_fix_detail = f"no must-fix defects on {vlabel}"
            elif unacknowledged:
                must_fix_detail = (
                    f"{unacknowledged} of {len(defects)} must-fix "
                    f"unacknowledged on {vlabel}"
                )
            else:
                must_fix_detail = (
                    f"all {len(defects)} must-fix acknowledged on {vlabel}"
                )
            automated("no_must_fix", unacknowledged == 0, must_fix_detail)

        # Every task needs a person's sign-off, bound to the version they
        # looked at. The tick records who signed and when.
        signoff = task_ticks.get((member.id, SIGNOFF_CHECK_KEY))
        if signoff is not None and version is not None and (
            signoff.task_version_id == version.id
        ):
            checks.append(
                _check(
                    SIGNOFF_CHECK_KEY,
                    passed=True,
                    detail=signoff.note,
                    kind="manual",
                    label="Signed off",
                    checked_by=signoff.checked_by_user_id,
                    checked_at=signoff.checked_at,
                )
            )
        else:
            checks.append(
                _check(
                    SIGNOFF_CHECK_KEY,
                    passed=False,
                    # An unchecked box already says "not signed off"; only a
                    # stale tick needs words.
                    detail=(
                        ""
                        if signoff is None
                        else "signed off on an older version; sign off again"
                    ),
                    kind="manual",
                    label="Signed off",
                )
            )

        for definition in config.manual:
            if definition.scope != "task":
                continue
            tick = task_ticks.get((member.id, definition.key))
            if tick is None:
                checks.append(
                    _check(
                        definition.key,
                        passed=False,
                        kind="manual",
                        label=definition.label,
                    )
                )
            elif version is not None and tick.task_version_id == version.id:
                checks.append(
                    _check(
                        definition.key,
                        passed=True,
                        detail=tick.note,
                        kind="manual",
                        label=definition.label,
                        checked_by=tick.checked_by_user_id,
                        checked_at=tick.checked_at,
                    )
                )
            else:
                checks.append(
                    _check(
                        definition.key,
                        passed=False,
                        detail="checked on an older version; re-attest",
                        kind="manual",
                        label=definition.label,
                    )
                )

        rows.append(
            DeliveryTaskBoardRow(
                delivery_task_id=member.id,
                task_id=member.task_id,
                task_name=task.name,
                version_id=version.id if version else None,
                version=version.version if version else None,
                pinned_version_id=member.pinned_version_id,
                newer_version_exists=bool(
                    version and max_versions.get(task.id, version.version)
                    > version.version
                ),
                is_visible=member.is_visible,
                sort_order=member.sort_order,
                customer_note=member.customer_note,
                internal_note=member.internal_note,
                checks=checks,
                defects=defects,
                ready=all(c.status in ("pass", "off", "waived") for c in checks),
            )
        )

    delivery_checks = []
    for definition in config.manual:
        if definition.scope != "delivery":
            continue
        tick = delivery_ticks.get(definition.key)
        delivery_checks.append(
            _check(
                definition.key,
                passed=tick is not None,
                detail=tick.note if tick else "",
                kind="manual",
                label=definition.label,
                checked_by=tick.checked_by_user_id if tick else None,
                checked_at=tick.checked_at if tick else None,
            )
        )

    ready_task_count = sum(1 for r in rows if r.ready)
    ready = (
        bool(rows)
        and ready_task_count == len(rows)
        and all(c.status == "pass" for c in delivery_checks)
    )
    return DeliveryBoardResponse(
        delivery=DeliveryResponse.model_validate(delivery),
        check_config=config,
        tasks=rows,
        delivery_checks=delivery_checks,
        ready=ready,
        ready_task_count=ready_task_count,
        task_count=len(rows),
        finalized_at=delivery.finalized_at,
    )


async def get_delivery_board_core(
    session: AsyncSession, *, delivery_id: str, org_id: str | None
) -> DeliveryBoardResponse:
    delivery = await _get_delivery(session, delivery_id, org_id)
    if delivery.status == "finalized":
        snapshot = await session.scalar(
            select(DeliverySnapshotModel)
            .where(DeliverySnapshotModel.delivery_id == delivery.id)
            .order_by(DeliverySnapshotModel.created_at.desc())
            .limit(1)
        )
        if snapshot is not None:
            board = DeliveryBoardResponse.model_validate(snapshot.snapshot["board"])
            board.frozen = True
            return board
    return await _compute_board(session, delivery)


# =============================================================================
# Finalize
# =============================================================================


def _customer_safe_board(board: DeliveryBoardResponse) -> dict:
    """The snapshot variant a future share page serves: no internal notes,
    no hidden tasks."""
    public = board.model_dump(mode="json")
    public["tasks"] = [
        {**row, "internal_note": None}
        for row in public["tasks"]
        if row["is_visible"]
    ]
    return public


async def finalize_delivery_core(
    session: AsyncSession,
    *,
    delivery_id: str,
    org_id: str | None,
    user_id: str | None,
) -> DeliveryBoardResponse:
    delivery = await _get_delivery(session, delivery_id, org_id, for_update=True)
    _require_active(delivery)
    board = await _compute_board(session, delivery)
    if not board.ready:
        blockers = [
            f"{row.task_name}: {c.label} — {c.detail or 'failing'}"
            for row in board.tasks
            for c in row.checks
            if c.status == "fail"
        ] + [
            f"delivery: {c.label} — {c.detail or 'failing'}"
            for c in board.delivery_checks
            if c.status == "fail"
        ]
        raise HTTPException(
            status_code=409,
            detail="delivery is not ready: " + "; ".join(blockers[:20]),
        )

    members = await _member_rows(session, delivery.id)
    version_by_task = {row.task_id: row.version_id for row in board.tasks}
    for member in members:
        member.pinned_version_id = version_by_task.get(member.task_id)

    now = utcnow()
    delivery.status = "finalized"
    delivery.finalized_at = now
    delivery.finalized_by_user_id = user_id
    board.frozen = True
    board.finalized_at = now
    board.delivery.status = "finalized"
    board.delivery.finalized_at = now

    session.add(
        DeliverySnapshotModel(
            delivery_id=delivery.id,
            snapshot={
                "board": board.model_dump(mode="json"),
                "public": _customer_safe_board(board),
            },
            scope=[
                {"task_id": row.task_id, "task_version_id": row.version_id}
                for row in board.tasks
            ],
            created_by_user_id=user_id,
        )
    )
    await session.flush()
    return board


# =============================================================================
# QA history
# =============================================================================


async def get_task_qa_history_core(
    session: AsyncSession, *, task_id: str, org_id: str | None
) -> TaskQAHistoryResponse:
    task = await session.get(TaskModel, task_id)
    if task is None or task.org_id != org_id:
        raise HTTPException(status_code=404, detail="task not found")

    versions = (
        await session.scalars(
            select(TaskVersionModel)
            .where(TaskVersionModel.task_id == task_id)
            .order_by(TaskVersionModel.version.desc())
        )
    ).all()

    scope = EligibleTrialScope(membership=[TrialModel.task_id == task_id])
    rollouts: dict[str, tuple[int, int]] = {
        version_id: (count, agents)
        for version_id, count, agents in (
            await session.execute(
                select(
                    TrialModel.task_version_id,
                    func.count(),
                    func.count(func.distinct(TrialModel.agent)),
                )
                .where(*scope.clauses(), TrialModel.status == TrialStatus.SUCCESS)
                .group_by(TrialModel.task_version_id)
            )
        ).all()
    }

    qa_trials = (
        await session.execute(
            select(
                TrialModel.id,
                TrialModel.kind,
                TrialModel.task_version_id,
                TrialModel.status,
                TrialModel.started_at,
                TrialModel.finished_at,
            )
            .where(TrialModel.task_id == task_id, TrialModel.kind.in_(["qa", "audit"]))
            .order_by(TrialModel.created_at.desc())
        )
    ).all()
    runs_by_version: dict[str | None, list[TaskQAHistoryRun]] = {}
    for trial_id, kind, version_id, status, started_at, finished_at in qa_trials:
        runs_by_version.setdefault(version_id, []).append(
            TaskQAHistoryRun(
                trial_id=trial_id,
                kind=kind,
                status=status.value if status else None,
                started_at=started_at,
                finished_at=finished_at,
            )
        )

    # The board's defect source, so history and board never disagree on
    # what counts as a must-fix (pre-trial items plus trial analyses).
    must_fix = await _must_fix_items(session, {v.id: v for v in versions})

    out = []
    for version in versions:
        count, agents = rollouts.get(version.id, (0, 0))
        out.append(
            TaskQAHistoryVersion(
                version_id=version.id,
                version=version.version,
                created_at=version.created_at,
                message=version.message,
                is_current=version.id == task.current_version_id,
                pre_trial_status=(
                    version.pre_trial_status.value
                    if version.pre_trial_status
                    else None
                ),
                pre_trial_finished_at=version.pre_trial_finished_at,
                must_fix=len(must_fix[version.id]),
                pre_trial_should_fix=sum(
                    1
                    for i in _pre_trial_items(version)
                    if i.get("tier") == "should_fix"
                ),
                rollout_count=count,
                rollout_agents=agents,
                qa_runs=runs_by_version.get(version.id, []),
            )
        )

    return TaskQAHistoryResponse(
        task_id=task.id,
        task_name=task.name,
        current_version_id=task.current_version_id,
        verdict=task.verdict if isinstance(task.verdict, dict) else None,
        verdict_status=task.verdict_status.value if task.verdict_status else None,
        versions=out,
    )
