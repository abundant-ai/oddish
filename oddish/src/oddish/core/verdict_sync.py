from __future__ import annotations

from typing import Any, Awaitable, Callable

from oddish.analyze import Classification, TrialClassification
from oddish.db import TaskModel, TaskStatus, TrialModel, VerdictStatus, get_session, utcnow


def build_verdict_payload(
    verdict: Any,
    classifications: list[TrialClassification],
) -> dict:
    """Render the dict stored on ``tasks.verdict``.

    ``verdict`` supplies only the model's judgment; the four counts are always
    recomputed from ``classifications`` so no model output can inflate them.
    Accepts both ``TaskVerdict`` and ``TaskVerdictModel`` by duck typing, which
    is what lets the legacy and AnalyzerBlock paths share one writer.
    """
    return {
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
    *synthesized* verdict, so the legacy and block paths cannot diverge.
    Cleanup and gate-failure paths elsewhere still set ``verdict_status``
    directly; they never produce a payload.

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
            task.verdict = payload
            task.verdict_status = VerdictStatus.SUCCESS
            task.verdict_error = None
        else:
            task.verdict_status = VerdictStatus.FAILED
            task.verdict_error = error or "Verdict synthesis failed with exception"

        task.verdict_finished_at = utcnow()
        # The task completes either way: a failed verdict must not leave the
        # task hanging in a non-terminal state.
        task.status = TaskStatus.COMPLETED
        task.finished_at = utcnow()
        return task.verdict_status.value


def build_pre_trial_payload(items: list) -> dict:
    """Render the dict stored on ``tasks.pre_trial``. Computes each item's
    stable id server-side (the LLM output omits it)."""
    from oddish.analyze.models import compute_action_item_id

    out = []
    for item in items:
        item.id = item.id or compute_action_item_id(item)
        out.append(item.model_dump(mode="json"))
    return {"items": out}


async def sync_pre_trial_to_task(
    task_id: str,
    *,
    payload: dict | None,
    error: BaseException | str | None,
    should_store: Callable[[Any], Awaitable[bool]] | None = None,
) -> None:
    """Write the pre-trial columns. Unlike :func:`sync_verdict_to_task`, this
    never completes the task and never touches a verdict column -- pre-trial
    is a task-source audit that runs independently of trial classification.
    """
    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        if task is None:
            return

        if should_store is not None and not await should_store(session):
            return

        if error is None:
            task.pre_trial = payload
            task.pre_trial_status = VerdictStatus.SUCCESS
            task.pre_trial_error = None
        else:
            task.pre_trial_status = VerdictStatus.FAILED
            task.pre_trial_error = str(error)

        task.pre_trial_finished_at = utcnow()


async def aggregate_exploited_into_pre_trial(task_id: str) -> None:
    """Stamp exploited=true (+ evidence) onto ``task.pre_trial`` items whose id
    was exploited in any trial. The "doubly note" elevation. Idempotent.
    """
    from sqlalchemy import select

    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        if task is None or not task.pre_trial:
            return

        trials = (
            await session.execute(select(TrialModel).where(TrialModel.task_id == task_id))
        ).scalars().all()

        exploited: dict[str, str] = {}
        for trial in trials:
            for a in (trial.analysis or {}).get("exploitation", []):
                if a.get("exploited") and a.get("links_to"):
                    exploited.setdefault(a["links_to"], a.get("exploit_evidence") or "")

        # Build fresh item dicts rather than mutating in place: the loaded
        # ``task.pre_trial`` dict is the very object SQLAlchemy diffs the
        # reassignment against, so mutating it before reassigning leaves the
        # "old" and "new" values equal by content and the UPDATE gets
        # skipped even though the column is reassigned.
        changed = False
        new_items = []
        for item in task.pre_trial.get("items", []):
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
            task.pre_trial = {**task.pre_trial, "items": new_items}
            await session.commit()
