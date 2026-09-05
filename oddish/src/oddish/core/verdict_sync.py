from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from oddish.analyze import Classification, TrialClassification
from oddish.analyze.models import TaskVerdictModel
from oddish.core.baseline_gate import GateOutcome, evaluate_baseline_gate
from oddish.core.verdict_state import (
    complete_verdict,
    complete_verdict_without_result,
    fail_verdict,
)
from oddish.db import (
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    VerdictStatus,
    get_session,
    utcnow,
)

logger = logging.getLogger(__name__)


def apply_deterministic_verdict_rules(
    verdict: TaskVerdictModel | None,
    *,
    must_fix_ids: list[str],
    baseline_evidence: list[dict],
) -> TaskVerdictModel | None:
    """Apply decisive server-owned evidence without asking the model to count."""
    if verdict is not None and not verdict.is_good:
        return verdict
    if baseline_evidence:
        outcome, _ = evaluate_baseline_gate(
            (item.get("agent"), item.get("reward")) for item in baseline_evidence
        )
        if outcome is GateOutcome.FAULTY:
            return TaskVerdictModel(
                verdict="reject",
                confidence="high",
                primary_issue="CRITICAL: The deterministic baseline validation failed.",
                recommendations=[
                    "Fix the nop/oracle baseline result before accepting the task."
                ],
                reasoning=(
                    "An oracle must pass and a nop agent must fail. The recorded "
                    "baseline results do not satisfy that rule."
                ),
            )
    if not must_fix_ids:
        return verdict
    count = len(must_fix_ids)
    noun = "finding" if count == 1 else "findings"
    return TaskVerdictModel(
        verdict="reject",
        confidence="high",
        primary_issue=f"The source audit reported {count} must-fix {noun}.",
        recommendations=[
            "Resolve every `must_fix` source-audit finding before accepting the task."
        ],
        reasoning=(
            "A `must_fix` source-audit finding can decide a trial, so successful "
            "solver runs cannot make the task acceptable."
        ),
    )


def build_verdict_payload(
    verdict: Any,
    classifications: list[TrialClassification],
) -> dict:
    """Render the dict stored on ``tasks.verdict``.

    ``verdict`` supplies only the model's judgment; the four counts are always
    recomputed from ``classifications`` so no model output can inflate them.
    """
    return {
        "verdict": "accept" if verdict.is_good else "reject",
        # Old rows and the SQL readers use is_good; keep it next to the label.
        "is_good": verdict.is_good,
        "confidence": verdict.confidence,
        "primary_issue": verdict.primary_issue,
        "reasoning": verdict.reasoning,
        "recommendations": list(verdict.recommendations),
        "task_problem_count": sum(1 for c in classifications if c.is_task_problem),
        "agent_problem_count": sum(
            1
            for c in classifications
            if c.classification == Classification.GOOD_FAILURE
        ),
        "success_count": sum(
            1
            for c in classifications
            if c.classification
            in (Classification.GOOD_SUCCESS, Classification.BAD_SUCCESS)
        ),
        "harness_error_count": sum(
            1
            for c in classifications
            if c.classification == Classification.HARNESS_ERROR
        ),
    }


async def sync_verdict_to_task(
    task_id: str,
    *,
    payload: dict | None,
    error: str | None,
    should_store: Callable[[Any], Awaitable[bool]] | None = None,
) -> str | None:
    """Write verdict state and complete the task. The only writer of a
    synthesized verdict.

    Returns the terminal ``VerdictStatus`` value written, or ``None`` when the
    write was skipped (task gone, or the job was cancelled).
    """
    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        if not task:
            return None

        if should_store is not None and not await should_store(session):
            return None

        if payload:
            complete_verdict(task, payload=payload, now=utcnow())
            terminal_status = VerdictStatus.SUCCESS
        else:
            failure = error or "Verdict synthesis failed with exception"
            fail_verdict(task, error=failure, now=utcnow())
            terminal_status = VerdictStatus.FAILED

        task.status = TaskStatus.COMPLETED
        task.finished_at = utcnow()
        return terminal_status.value


async def complete_task_without_verdict(
    task_id: str,
    *,
    should_store: Callable[[Any], Awaitable[bool]] | None = None,
) -> str | None:
    """Finish a classification-only QA pass without a current verdict.

    Per-trial analysis is already stored; this only clears the in-flight
    verdict state and completes the task. Prior QA artifacts remain in trial
    storage, but their verdict must not describe the newly classified set.
    """
    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        if not task:
            return None
        if should_store is not None and not await should_store(session):
            return None
        complete_verdict_without_result(task, now=utcnow())
        task.status = TaskStatus.COMPLETED
        task.finished_at = utcnow()
        return VerdictStatus.SUCCESS.value


def build_pre_trial_payload(
    items: list,
    *,
    cost_usd: float | None = None,
    block_id: str | None = None,
    audit_policy_hash: str | None = None,
) -> dict:
    """Render the dict stored on ``task_versions.pre_trial``. Computes each
    item's stable id server-side (the LLM output omits it).

    ``cost_usd`` and ``block_id`` are recorded here because they are only
    knowable at write time. ``audit_policy_hash`` identifies the exact bundled
    audit policy that produced the findings. Omitted keys mean "not captured",
    which is how older rows read.
    """
    from oddish.analyze.models import compute_action_item_id

    out = []
    for item in items:
        item.id = item.id or compute_action_item_id(item)
        out.append(item.model_dump(mode="json"))
    payload: dict = {"items": out}
    if cost_usd is not None:
        payload["cost_usd"] = cost_usd
    if block_id is not None:
        payload["block_id"] = block_id
    if audit_policy_hash is not None:
        payload["audit_policy_hash"] = audit_policy_hash
    return payload


async def sync_pre_trial_to_task_version(
    task_version_id: str,
    *,
    payload: dict | None,
    error: BaseException | str | None,
    expected_content_hash: str | None = None,
    expected_audit_trial_id: str | None = None,
) -> str | None:
    """Write the pre-trial columns on the audited task version. Unlike
    :func:`sync_verdict_to_task`, this never completes the task and never
    touches a verdict column -- pre-trial is a per-version source audit that
    runs independently of trial classification.

    ``expected_content_hash`` pins the source bytes the audit actually read:
    the check runs here, under the version row lock, because an in-place
    overwrite can replace the bytes between any earlier unlocked check and
    this write. On a mismatch nothing is written -- the overwrite already
    reset the pre-trial state, so a fresh audit of the new bytes can still
    be enqueued.

    Returns the terminal ``VerdictStatus`` value written, or ``None`` when
    the write was skipped (version gone, or overwritten bytes) so the caller
    can release its claim on the version.
    """
    async with get_session() as session:
        version = await session.get(
            TaskVersionModel, task_version_id, with_for_update=True
        )
        if version is None:
            return None
        if expected_audit_trial_id is not None:
            from oddish.core.endpoints._common import USER_CANCELLED_MESSAGE

            if (
                version.pre_trial_status == VerdictStatus.FAILED
                and version.pre_trial_error == USER_CANCELLED_MESSAGE
            ):
                return None
            imported = await session.get(TrialModel, expected_audit_trial_id)
            if imported is None or imported.harbor_stage == "cancelled":
                return None
            latest = await session.scalar(
                select(TrialModel.id)
                .where(
                    TrialModel.task_version_id == task_version_id,
                    TrialModel.kind == "audit",
                    TrialModel.deleted_at.is_(None),
                    TrialModel.superseded_by_trial_id.is_(None),
                )
                .order_by(TrialModel.created_at.desc(), TrialModel.id.desc())
                .limit(1)
            )
            if latest != expected_audit_trial_id:
                return None
        if (
            expected_content_hash is not None
            and version.content_hash is not None
            and version.content_hash != expected_content_hash
        ):
            logger.warning(
                "pre-trial write for version %s skipped: content hash changed "
                "since the audit started (in-place overwrite)",
                task_version_id,
            )
            return None

        if (
            error is None
            and expected_audit_trial_id is not None
            and version.pre_trial_status == VerdictStatus.SUCCESS
            and (version.pre_trial or {}).get("block_id") == expected_audit_trial_id
        ):
            # Re-importing the same immutable audit must not change its
            # fingerprint or erase later exploitation annotations.
            return VerdictStatus.SUCCESS.value
        if error is None:
            version.pre_trial = payload
            version.pre_trial_status = VerdictStatus.SUCCESS
            version.pre_trial_error = None
        else:
            # Cleared so a payload is only ever paired with SUCCESS. The claim
            # makes SUCCESS terminal, so nothing good is being thrown away.
            version.pre_trial = None
            version.pre_trial_status = VerdictStatus.FAILED
            version.pre_trial_error = str(error)

        version.pre_trial_finished_at = utcnow()
        return version.pre_trial_status.value


async def aggregate_exploited_into_pre_trial(task_id: str) -> None:
    """Stamp exploited=true (+ evidence) onto ``task_versions.pre_trial`` items
    whose id was exploited in any trial. The "doubly note" elevation.
    Idempotent.

    Item ids are content hashes computed per audit, so each exploited
    ``links_to`` id matches at most one version's items; ids that match no
    version at all are logged.
    """
    async with get_session() as session:
        versions = (
            (
                await session.execute(
                    select(TaskVersionModel)
                    .where(TaskVersionModel.task_id == task_id)
                    .where(TaskVersionModel.pre_trial.isnot(None))
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if not versions:
            return

        # Intentionally reads ALL trials, unlike the verdict path which filters
        # ``superseded_by_trial_id.is_(None)``: a superseded/retried trial that
        # exploited the weakness is still valid evidence the task-source flaw is
        # exploitable. This is a task-level audit, not the current-trial verdict.
        trials = (
            (
                await session.execute(
                    select(TrialModel).where(TrialModel.task_id == task_id)
                )
            )
            .scalars()
            .all()
        )

        exploited: dict[str, str] = {}
        for trial in trials:
            for a in (trial.analysis or {}).get("exploitation", []):
                if a.get("exploited") and a.get("links_to"):
                    exploited.setdefault(a["links_to"], a.get("exploit_evidence") or "")

        known_ids = {
            item.get("id")
            for version in versions
            for item in (version.pre_trial or {}).get("items", [])
        }
        unmatched = sorted(set(exploited) - known_ids)
        if unmatched:
            logger.warning(
                "task %s: exploited links_to id(s) %s did not match any pre_trial item id",
                task_id,
                unmatched,
            )

        any_changed = False
        for version in versions:
            # Build fresh item dicts rather than mutating in place: the loaded
            # ``pre_trial`` dict is the very object SQLAlchemy diffs the
            # reassignment against, so mutating it before reassigning leaves the
            # "old" and "new" values equal by content and the UPDATE gets
            # skipped even though the column is reassigned.
            changed = False
            new_items = []
            for item in (version.pre_trial or {}).get("items", []):
                item_id = item.get("id")
                if item_id not in exploited:
                    new_items.append(item)
                    continue
                evidence = exploited[item_id]
                new_item = dict(item)
                if not new_item.get("exploited"):
                    changed = True
                new_item["exploited"] = True
                if evidence and new_item.get("exploit_evidence") != evidence:
                    new_item["exploit_evidence"] = evidence
                    changed = True
                new_items.append(new_item)

            if changed:
                # In-place mutation of a JSONB dict is NOT auto-detected by
                # SQLAlchemy; reassigning the column is what marks it dirty.
                version.pre_trial = {**version.pre_trial, "items": new_items}
                any_changed = True

        if any_changed:
            await session.commit()
