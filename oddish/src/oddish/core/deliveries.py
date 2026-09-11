"""Delivery checklists: is this set of tasks good to ship? (docs/delivery-design.md)

Readiness is derived at read time, never stored. Every automated check
evaluates the task's *current default version* (``tasks.current_version_id``),
so publishing a new version resets the board on its own. Manual ticks record
the version they attested to and only count while it is still the default.
"""

from __future__ import annotations

from typing import Any, Sequence

from fastapi import HTTPException
from sqlalchemy import and_, case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, load_only

from oddish.core.delivery_qa import delivery_qa_statuses
from oddish.core.delivery_progress import (
    delivery_progress_history,
    record_delivery_progress,
)
from oddish.core.task_findings import pre_trial_items, task_defect_items
from oddish.db import (
    CustomerModel,
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
    DeliveryQAStatus,
    DeliveryResponse,
    DeliveryTaskBoardRow,
    DeliveryTasksAdd,
    ManualCheckSet,
    QAWorkClaim,
    QAWorkMetadata,
    QAWorkPatch,
    TaskQAHistoryDecision,
    TaskQAHistoryFinding,
    TaskQAHistoryResponse,
    TaskQAHistoryRun,
    TaskQAHistoryVersion,
)

# The automated checks a delivery can run, with their default parameters.
# ``check_config["automated"]`` merges over these per key; unknown keys are
# rejected at write time so a typo cannot silently disable a check.
# Delivery minimums are independent of verdict generation.
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
    "pre_trial_passed": "Source review completed",
    "min_rollouts": "Enough rollouts",
    "verdict_ok": "No blocking defects in verdict",
    "no_must_fix": "Every defect resolved or acknowledged",
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
    # This requirement cannot be disabled, including by legacy configuration.
    merged["no_must_fix"]["enabled"] = True
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
        DeliveryModel,
        delivery_id,
        options=[joinedload(DeliveryModel.customer)],
        with_for_update={"of": DeliveryModel} if for_update else None,
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


async def _find_customer(
    session: AsyncSession, org_id: str | None, ref: str
) -> CustomerModel | None:
    return await session.scalar(
        select(CustomerModel).where(
            CustomerModel.org_id == org_id,
            or_(CustomerModel.id == ref, CustomerModel.name == ref),
        )
    )


async def _resolve_customer(
    session: AsyncSession, org_id: str | None, ref: str
) -> CustomerModel:
    """The customer this delivery ships to: by id, by name, or created.

    A new name creates the customer row in place, so requiring a customer
    never forces a separate setup step."""
    ref = ref.strip()
    customer = await _find_customer(session, org_id, ref)
    if customer is None:
        # The savepoint keeps the unique (org, name) race survivable: a
        # concurrent create of the same name loses the insert but keeps
        # the outer transaction, and uses the winner's row.
        try:
            async with session.begin_nested():
                customer = CustomerModel(org_id=org_id, name=ref)
                session.add(customer)
        except IntegrityError:
            customer = await _find_customer(session, org_id, ref)
            if customer is None:
                raise
    return customer


async def create_customer_core(
    session: AsyncSession, *, org_id: str | None, name: str
) -> CustomerModel:
    """Explicit customer creation, for the customers form. A duplicate
    name is a conflict here, unlike the get-or-create on delivery create."""
    name = name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="customer name is required")
    existing = await session.scalar(
        select(CustomerModel).where(
            CustomerModel.org_id == org_id, CustomerModel.name == name
        )
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"customer '{name}' already exists")
    # Same savepoint guard as _resolve_customer: a concurrent create of
    # the same name is a conflict here, not a 500.
    try:
        async with session.begin_nested():
            customer = CustomerModel(org_id=org_id, name=name)
            session.add(customer)
    except IntegrityError:
        raise HTTPException(
            status_code=409, detail=f"customer '{name}' already exists"
        ) from None
    return customer


async def list_customers_core(
    session: AsyncSession, *, org_id: str | None
) -> list[CustomerModel]:
    return list(
        (
            await session.scalars(
                select(CustomerModel)
                .where(CustomerModel.org_id == org_id)
                .order_by(CustomerModel.name)
            )
        ).all()
    )


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
    customer = await _resolve_customer(session, org_id, data.customer)
    delivery = DeliveryModel(
        org_id=org_id,
        created_by_user_id=user_id,
        name=data.name,
        customer_id=customer.id,
        description=data.description,
        check_config=check_config.model_dump() if check_config else {},
    )
    session.add(delivery)
    await session.flush()
    await session.refresh(delivery, ["customer"])
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
    if data.customer is not None:
        customer = await _resolve_customer(session, org_id, data.customer)
        delivery.customer_id = customer.id
        await session.refresh(delivery, ["customer"])
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


def _distinct_agent_count():
    """Distinct agents for delivery: trimmed, lowercased, blanks ignored."""
    return func.count(
        func.distinct(func.nullif(func.trim(func.lower(TrialModel.agent)), ""))
    )


def _verdict_qa_clauses() -> list:
    """Filters for QA runs that can author ``tasks.verdict``.

    Classification-only runs carry with_verdict=false and cannot vouch for
    the version they graded. This includes historical runs below the former
    evidence threshold and runs whose source audit was unavailable.
    """
    return [
        TrialModel.kind == "qa",
        TrialModel.status == TrialStatus.SUCCESS,
        TrialModel.task_version_id.isnot(None),
        func.coalesce(
            TrialModel.harbor_config["analysis_payload"].op("->>")("with_verdict"),
            "true",
        )
        != "false",
    ]


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
                    "acknowledge each must-fix defect on its own with 'ack:<defect-id>'"
                ),
            )
        if check_key not in WAIVABLE_CHECKS:
            raise HTTPException(
                status_code=404,
                detail=f"'{check_key}' is not an automated check",
            )
        return
    if key.startswith(ACK_CHECK_PREFIX):
        defects = (await task_defect_items(session, {version.id: version}))[version.id]
        if key[len(ACK_CHECK_PREFIX) :] not in {d["id"] for d in defects}:
            raise HTTPException(
                status_code=404,
                detail="defect not found on the task's current version",
            )
        return
    # Sign-off: judge the same board the reader sees, so the rule cannot
    # drift from the display. Unacked defects and unwaived failing checks
    # both refuse it.
    board = await _compute_board(session, delivery, task_ids=[member.task_id])
    row = next((r for r in board.tasks if r.delivery_task_id == member.id), None)
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
    is_decision = (
        key == SIGNOFF_CHECK_KEY
        or key.startswith(ACK_CHECK_PREFIX)
        or key.startswith(WAIVE_CHECK_PREFIX)
    )
    if is_decision:
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
        # Fetch membership and lock the task's default version together. The
        # delivery lock still serializes this decision against finalization.
        membership = (
            await session.execute(
                select(DeliveryTaskModel, TaskModel.current_version_id)
                .join(TaskModel, TaskModel.id == DeliveryTaskModel.task_id)
                .where(
                    DeliveryTaskModel.id == data.delivery_task_id,
                    DeliveryTaskModel.delivery_id == delivery.id,
                )
                .with_for_update(of=TaskModel)
            )
        ).one_or_none()
        if membership is None:
            raise HTTPException(status_code=404, detail="task not in this delivery")
        member, task_version_id = membership
        if (
            data.checked
            and is_decision
            and (data.expected_version_id is None or user_id is None)
        ):
            raise HTTPException(
                status_code=422,
                detail="expected_version_id and an authenticated person are required for acknowledgment or sign-off",
            )
        # The tick attests to the content the human looked at: the task's
        # current default version. A later version change un-ticks it.
        if (
            "expected_version_id" in data.model_fields_set
            and data.expected_version_id != task_version_id
        ):
            raise HTTPException(
                status_code=409,
                detail="The selected task version changed. Refresh the delivery and review the new version.",
            )
        if data.checked and is_decision:
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
            DeliveryManualCheckModel.task_version_id == task_version_id,
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


async def claim_delivery_qa_core(
    session: AsyncSession,
    *,
    delivery_id: str,
    org_id: str | None,
    user_id: str,
    data: QAWorkClaim,
) -> list[str]:
    delivery = await _get_delivery(session, delivery_id, org_id, for_update=True)
    _require_active(delivery)
    # The version lock also arbitrates claims made through another delivery.
    # Candidate version IDs keep a stale browser from claiming a new version.
    versions = (
        await session.scalars(
            select(TaskVersionModel)
            .join(TaskModel, TaskModel.current_version_id == TaskVersionModel.id)
            .join(DeliveryTaskModel, DeliveryTaskModel.task_id == TaskModel.id)
            .where(
                DeliveryTaskModel.delivery_id == delivery_id,
                TaskModel.org_id == org_id,
                TaskVersionModel.id.in_(data.version_ids),
                TaskVersionModel.qa_work["owner_user_id"].astext.is_(None),
            )
            .order_by(
                case(
                    {
                        version_id: index
                        for index, version_id in enumerate(data.version_ids)
                    },
                    value=TaskVersionModel.id,
                )
            )
            .limit(data.limit)
            .with_for_update(of=TaskVersionModel, skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).all()
    for version in versions:
        work = QAWorkMetadata.model_validate(version.qa_work or {})
        work.owner_user_id, work.claimed_at = user_id, utcnow()
        version.qa_work = work.model_dump(mode="json")
    await session.flush()
    return [version.id for version in versions]


async def patch_delivery_qa_work_core(
    session: AsyncSession,
    *,
    delivery_id: str,
    org_id: str | None,
    user_id: str,
    is_admin: bool,
    data: QAWorkPatch,
) -> None:
    delivery = await _get_delivery(session, delivery_id, org_id, for_update=True)
    _require_active(delivery)
    version = await session.scalar(
        select(TaskVersionModel)
        .join(TaskModel, TaskModel.current_version_id == TaskVersionModel.id)
        .join(DeliveryTaskModel, DeliveryTaskModel.task_id == TaskModel.id)
        .where(
            DeliveryTaskModel.delivery_id == delivery_id,
            TaskModel.org_id == org_id,
            TaskVersionModel.id == data.version_id,
        )
        .with_for_update(of=TaskVersionModel)
        .execution_options(populate_existing=True)
    )
    if version is None:
        raise HTTPException(
            status_code=409,
            detail="Task version changed or left this delivery; refresh the board",
        )
    work = QAWorkMetadata.model_validate(version.qa_work or {})
    if work.owner_user_id != user_id and not is_admin:
        raise HTTPException(
            status_code=403, detail="Claim this task before editing its QA work"
        )
    if data.release:
        work.owner_user_id, work.claimed_at = None, None
    if data.issue_categories is not None:
        work.issue_categories = list(dict.fromkeys(data.issue_categories))
    if data.note is not None:
        work.note = data.note
    version.qa_work = work.model_dump(mode="json")
    await session.flush()


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
    session: AsyncSession,
    delivery: DeliveryModel,
    *,
    task_ids: Sequence[str] | None = None,
    include_details: bool = True,
) -> DeliveryBoardResponse:
    config = _normalized_check_config(delivery.check_config)
    auto = config.automated
    # Keep missing/deleted tasks as failing members. Only live tasks supply
    # current-version evidence; joining these scalar relations cannot multiply
    # membership rows the way joining trials or findings would.
    member_scope = (
        select(DeliveryTaskModel.task_id)
        .where(DeliveryTaskModel.delivery_id == delivery.id)
        .correlate(None)
    )
    if task_ids is not None:
        member_scope = member_scope.where(DeliveryTaskModel.task_id.in_(task_ids))
    version_scope = (
        select(TaskModel.current_version_id)
        .where(TaskModel.id.in_(member_scope), TaskModel.deleted_at.is_(None))
        .correlate(None)
    )
    # Each derived table has at most one row per task/version. Joining raw
    # trials, versions and ticks would multiply rows and inflate the counts.
    rollouts_query = (
        select(
            TrialModel.task_version_id.label("version_id"),
            func.count().label("count"),
            _distinct_agent_count().label("agents"),
        )
        .where(
            *EligibleTrialScope(
                membership=[TrialModel.task_version_id.in_(version_scope)]
            ).clauses(),
            TrialModel.status == TrialStatus.SUCCESS,
        )
        .group_by(TrialModel.task_version_id)
        .subquery()
    )
    latest_verdict = (
        select(TrialModel.task_id, TrialModel.task_version_id)
        .where(
            TrialModel.task_id.in_(member_scope),
            TrialModel.deleted_at.is_(None),
            *_verdict_qa_clauses(),
        )
        .distinct(TrialModel.task_id)
        .order_by(
            TrialModel.task_id,
            func.coalesce(TrialModel.finished_at, TrialModel.created_at).desc(),
            TrialModel.created_at.desc(),
            TrialModel.id.desc(),
        )
        .subquery()
    )
    highest_versions = (
        select(
            TaskVersionModel.task_id,
            func.max(TaskVersionModel.version).label("highest"),
        )
        .where(
            TaskVersionModel.task_id.in_(member_scope),
            TaskVersionModel.deleted_at.is_(None),
        )
        .group_by(TaskVersionModel.task_id)
        .subquery()
    )
    member_rows = (
        await session.execute(
            select(
                DeliveryTaskModel,
                TaskModel,
                TaskVersionModel,
                rollouts_query.c.count,
                rollouts_query.c.agents,
                latest_verdict.c.task_version_id,
                highest_versions.c.highest,
            )
            .outerjoin(TaskModel, TaskModel.id == DeliveryTaskModel.task_id)
            .outerjoin(
                TaskVersionModel,
                and_(
                    TaskVersionModel.id == TaskModel.current_version_id,
                    TaskModel.deleted_at.is_(None),
                ),
            )
            .outerjoin(
                rollouts_query, rollouts_query.c.version_id == TaskVersionModel.id
            )
            .outerjoin(latest_verdict, latest_verdict.c.task_id == TaskModel.id)
            .outerjoin(highest_versions, highest_versions.c.task_id == TaskModel.id)
            .where(
                DeliveryTaskModel.delivery_id == delivery.id,
                DeliveryTaskModel.task_id.in_(member_scope),
            )
            .options(
                load_only(
                    TaskModel.id,
                    TaskModel.name,
                    TaskModel.current_version_id,
                    TaskModel.deleted_at,
                    TaskModel.verdict,
                    TaskModel.verdict_status,
                ),
                load_only(
                    TaskVersionModel.id,
                    TaskVersionModel.task_id,
                    TaskVersionModel.version,
                    TaskVersionModel.pre_trial,
                    TaskVersionModel.reported_findings,
                    TaskVersionModel.pre_trial_status,
                    TaskVersionModel.content_hash,
                    TaskVersionModel.pre_trial_started_at,
                    TaskVersionModel.pre_trial_finished_at,
                    TaskVersionModel.qa_work,
                ),
            )
            .order_by(
                DeliveryTaskModel.sort_order,
                DeliveryTaskModel.created_at,
                DeliveryTaskModel.id,
            )
            .execution_options(include_deleted=True)
        )
    ).all()
    members = []
    tasks: dict[str, TaskModel] = {}
    versions: dict[str, TaskVersionModel] = {}
    max_versions: dict[str, int] = {}
    rollouts: dict[str, tuple[int, int]] = {}
    latest_qa_version: dict[str, str] = {}
    for member, task, version, count, agents, qa_version, highest in member_rows:
        members.append(member)
        if task is not None:
            tasks[task.id] = task
            if highest is not None:
                max_versions[task.id] = highest
            if qa_version is not None:
                latest_qa_version[task.id] = qa_version
        if version is not None:
            versions[version.id] = version
            rollouts[version.id] = (count or 0, agents or 0)
    must_fix_items = await task_defect_items(
        session, versions, include_details=include_details
    )

    qa_statuses = await delivery_qa_statuses(session, tasks=tasks, versions=versions)

    ticks = (
        await session.scalars(
            select(DeliveryManualCheckModel).where(
                DeliveryManualCheckModel.delivery_id == delivery.id,
                or_(
                    DeliveryManualCheckModel.delivery_task_id.is_(None),
                    DeliveryManualCheckModel.delivery_task_id.in_(
                        [m.id for m in members]
                    ),
                ),
            )
        )
    ).all()
    task_ticks = {
        (t.delivery_task_id, t.check_key, t.task_version_id): t
        for t in ticks
        if t.delivery_task_id
    }
    delivery_ticks = {t.check_key: t for t in ticks if t.delivery_task_id is None}
    previous_ticks = {(t.delivery_task_id, t.check_key) for t in ticks}

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
                            detail=("task was deleted; remove it from this delivery"),
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
            if not passed and version is not None and key in WAIVABLE_CHECKS:
                # A person may ship a red check anyway, but the override is
                # recorded and bound to the version they looked at.
                waive = task_ticks.get(
                    (member.id, WAIVE_CHECK_PREFIX + key, version.id)
                )
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
                ack = task_ticks.get(
                    (member.id, ACK_CHECK_PREFIX + item["id"], version.id)
                )
                acknowledged = ack is not None and ack.task_version_id == version.id
                defects.append(
                    DeliveryDefect(
                        id=item["id"],
                        title=item["title"],
                        source=item["source"],
                        finding_id=(
                            str(
                                item["finding"].get("links_to") or item["finding"]["id"]
                            )
                            if item["finding"].get("links_to")
                            or item["finding"].get("id") is not None
                            else None
                        ),
                        file=item["finding"].get("file"),
                        line_start=item["finding"].get("line_start"),
                        line_end=item["finding"].get("line_end"),
                        recorded_tier=item["recorded_tier"],
                        finding=item["finding"],
                        reporting_trial_id=item.get("reporting_trial_id"),
                        review_trial_id=item.get("review_trial_id"),
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
                f"source review completed on {vlabel}; defect checks are separate"
                if audited
                else f"source review {version.pre_trial_status.value.lower() if version.pre_trial_status else 'not run'} on {vlabel}; task quality not established by this review",
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
                automated(
                    "verdict_ok",
                    False,
                    f"no completed execution-review verdict on {vlabel}",
                )
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
                    "review found no blocking defects; human sign-off is separate"
                    if accepted
                    else f"blocking defect: {verdict.get('primary_issue') or ''}",
                )

            unacknowledged = sum(1 for d in defects if not d.acknowledged)
            if not defects:
                must_fix_detail = f"no reported task defects on {vlabel}; review completion checked separately"
            elif unacknowledged:
                must_fix_detail = (
                    f"{unacknowledged} of {len(defects)} task defects "
                    f"unacknowledged on {vlabel}"
                )
            else:
                must_fix_detail = f"all {len(defects)} task defects acknowledged as exceptions on {vlabel}"
            historical_unacknowledged = sum(
                d.recorded_tier != "must_fix" and not d.acknowledged for d in defects
            )
            if historical_unacknowledged:
                must_fix_detail += (
                    f"; {historical_unacknowledged} historically lower-severity findings "
                    "still require "
                    "individual acknowledgment; the recorded review is unchanged"
                )
            automated("no_must_fix", unacknowledged == 0, must_fix_detail)

        # Every task needs a person's sign-off, bound to the version they
        # looked at. The tick records who signed and when.
        signoff = task_ticks.get(
            (member.id, SIGNOFF_CHECK_KEY, version.id if version else None)
        )
        if (
            signoff is not None
            and version is not None
            and (signoff.task_version_id == version.id)
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
                    # Name the version still awaiting a human commitment.
                    detail=(
                        "signed off on an older version; sign off again"
                        if (member.id, SIGNOFF_CHECK_KEY) in previous_ticks
                        else (
                            f"awaiting sign-off on v{version.version}"
                            if version
                            else "awaiting sign-off"
                        )
                    ),
                    kind="manual",
                    label="Signed off",
                )
            )

        for definition in config.manual:
            if definition.scope != "task":
                continue
            tick = task_ticks.get(
                (member.id, definition.key, version.id if version else None)
            )
            if tick is None:
                checks.append(
                    _check(
                        definition.key,
                        passed=False,
                        kind="manual",
                        label=definition.label,
                        detail="checked on an older version; re-attest"
                        if (member.id, definition.key) in previous_ticks
                        else "",
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
                    version
                    and max_versions.get(task.id, version.version) > version.version
                ),
                is_visible=member.is_visible,
                sort_order=member.sort_order,
                customer_note=member.customer_note,
                internal_note=member.internal_note,
                checks=checks,
                defects=defects,
                qa=qa_statuses.get(task.id, DeliveryQAStatus()),
                qa_work=QAWorkMetadata.model_validate(version.qa_work or {})
                if version
                else QAWorkMetadata(),
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
        qa_as_of=utcnow(),
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
    session: AsyncSession,
    *,
    delivery_id: str,
    org_id: str | None,
    include_details: bool = True,
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
    board = await _compute_board(session, delivery, include_details=include_details)
    board.progress_history = await delivery_progress_history(session, delivery.id)
    return board


# =============================================================================
# Finalize
# =============================================================================


def _customer_safe_board(board: DeliveryBoardResponse) -> dict:
    """The snapshot variant a future share page serves: no internal notes,
    no hidden tasks."""
    public = board.model_dump(mode="json")
    public.pop("qa_viewer_user_id", None)
    public.pop("progress_history", None)
    public["tasks"] = [
        {
            **{
                key: value
                for key, value in row.items()
                if key not in {"qa", "qa_work", "qa_owner_name"}
            },
            "internal_note": None,
        }
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
    # The snapshot board is what finalized reads serve; it must carry the
    # pins too, not only the membership rows.
    for row in board.tasks:
        row.pinned_version_id = row.version_id

    now = utcnow()
    await record_delivery_progress(session, board, recorded_at=now)
    board.progress_history = await delivery_progress_history(session, delivery.id)
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
    # A task id or a task name, like every other delivery entry point.
    # An id match wins if a string happens to be both.
    matches = (
        await session.scalars(
            select(TaskModel).where(
                TaskModel.org_id == org_id,
                or_(TaskModel.id == task_id, TaskModel.name == task_id),
            )
        )
    ).all()
    task = next((t for t in matches if t.id == task_id), None) or next(
        iter(matches), None
    )
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    task_id = task.id

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
                    _distinct_agent_count(),
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
                TrialModel.error_message,
                TrialModel.analysis_error,
            )
            .where(TrialModel.task_id == task_id, TrialModel.kind.in_(["qa", "audit"]))
            .order_by(TrialModel.created_at.desc())
        )
    ).all()
    runs_by_version: dict[str | None, list[TaskQAHistoryRun]] = {}
    for (
        trial_id,
        kind,
        version_id,
        status,
        started_at,
        finished_at,
        error_message,
        analysis_error,
    ) in qa_trials:
        runs_by_version.setdefault(version_id, []).append(
            TaskQAHistoryRun(
                trial_id=trial_id,
                kind=kind,
                status=status.value if status else None,
                started_at=started_at,
                finished_at=finished_at,
                error=error_message or analysis_error,
            )
        )

    # The board's defect source, so history and board never disagree on
    # what counts as a must-fix (pre-trial items plus trial analyses).
    must_fix = await task_defect_items(session, {v.id: v for v in versions})

    # Which version the stored verdict covers: the one graded by the
    # newest verdict-producing QA run (same rule as the board).
    verdict_version_id = await session.scalar(
        select(TrialModel.task_version_id)
        .where(TrialModel.task_id == task_id, *_verdict_qa_clauses())
        .order_by(
            func.coalesce(TrialModel.finished_at, TrialModel.created_at).desc(),
            TrialModel.created_at.desc(),
            TrialModel.id.desc(),
        )
        .limit(1)
    )

    decisions_by_version: dict[str, list[TaskQAHistoryDecision]] = {}
    decisions = await session.scalars(
        select(DeliveryManualCheckModel)
        .join(
            DeliveryTaskModel,
            DeliveryTaskModel.id == DeliveryManualCheckModel.delivery_task_id,
        )
        .where(DeliveryTaskModel.task_id == task_id)
        .order_by(DeliveryManualCheckModel.checked_at, DeliveryManualCheckModel.id)
    )
    for decision in decisions:
        if decision.task_version_id:
            decisions_by_version.setdefault(decision.task_version_id, []).append(
                TaskQAHistoryDecision.model_validate(decision)
            )

    out = []
    for version in versions:
        count, agents = rollouts.get(version.id, (0, 0))
        findings = [
            TaskQAHistoryFinding(
                tier=item["recorded_tier"], title=item["title"], source=item["source"]
            )
            for item in must_fix[version.id]
        ]
        out.append(
            TaskQAHistoryVersion(
                version_id=version.id,
                version=version.version,
                created_at=version.created_at,
                message=version.message,
                is_current=version.id == task.current_version_id,
                pre_trial_status=(
                    version.pre_trial_status.value if version.pre_trial_status else None
                ),
                pre_trial_finished_at=version.pre_trial_finished_at,
                pre_trial_error=version.pre_trial_error,
                must_fix=len(must_fix[version.id]),
                pre_trial_should_fix=sum(
                    1 for i in pre_trial_items(version) if i.get("tier") == "should_fix"
                ),
                rollout_count=count,
                rollout_agents=agents,
                qa_runs=runs_by_version.get(version.id, []),
                findings=findings,
                decisions=decisions_by_version.get(version.id, []),
            )
        )

    return TaskQAHistoryResponse(
        task_id=task.id,
        task_name=task.name,
        current_version_id=task.current_version_id,
        verdict=task.verdict if isinstance(task.verdict, dict) else None,
        verdict_status=task.verdict_status.value if task.verdict_status else None,
        verdict_version_id=verdict_version_id,
        versions=out,
        # Runs whose trial carries no version id (legacy data). They belong
        # to no version row, but hiding them would understate the QA record.
        unversioned_runs=runs_by_version.get(None, []),
    )
