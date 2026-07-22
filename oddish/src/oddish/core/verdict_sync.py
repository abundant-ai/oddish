from __future__ import annotations

from typing import Any, Awaitable, Callable

from oddish.analyze import Classification, TrialClassification
from oddish.db import TaskModel, TaskStatus, VerdictStatus, get_session, utcnow


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
