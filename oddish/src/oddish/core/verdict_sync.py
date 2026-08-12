from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from sqlalchemy import select

from oddish.analyze import Classification, TrialClassification
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
    """Finish a QA pass that was not asked for a verdict (too few trials).

    Per-trial analysis is already stored; this only clears the in-flight
    verdict state and completes the task. A previously published verdict is
    restored rather than dropped.
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
    items: list, *, cost_usd: float | None = None, block_id: str | None = None
) -> dict:
    """Render the dict stored on ``task_versions.pre_trial``. Computes each
    item's stable id server-side (the LLM output omits it).

    ``cost_usd``/``block_id`` are recorded here because they are only knowable
    at write time: ``analysis_costs`` rows carry no block or version reference,
    so an audit's spend cannot be re-derived per version afterwards (a task with
    several audits has several cost rows and no way to tell them apart). Omitted
    keys mean "not captured", which is how the rows backfilled before this
    existed read.
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
    return payload


async def sync_pre_trial_to_task_version(
    task_version_id: str,
    *,
    payload: dict | None,
    error: BaseException | str | None,
) -> str | None:
    """Write the pre-trial columns on the audited task version. Unlike
    :func:`sync_verdict_to_task`, this never completes the task and never
    touches a verdict column -- pre-trial is a per-version source audit that
    runs independently of trial classification.

    Returns the terminal ``VerdictStatus`` value written, or ``None`` when
    the write was skipped (version gone) so the caller can release its claim
    on the version.
    """
    async with get_session() as session:
        version = await session.get(
            TaskVersionModel, task_version_id, with_for_update=True
        )
        if version is None:
            return None

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
