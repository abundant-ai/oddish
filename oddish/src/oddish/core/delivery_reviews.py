"""Stage-specific delivery review facts; never used to decide readiness."""

from oddish.db import TaskModel, TaskVersionModel, VerdictStatus
from oddish.schemas import DeliveryQAStatus, DeliveryReviews, DeliveryReviewStage


def delivery_reviews(
    task: TaskModel,
    version: TaskVersionModel | None,
    qa: DeliveryQAStatus,
    verdict_version_id: str | None,
) -> DeliveryReviews:
    source_status = (
        version.pre_trial_status.value.lower()
        if version and version.pre_trial_status
        else "not_run"
    )
    source = DeliveryReviewStage(
        status="completed" if source_status == "success" else source_status,
        finished_at=version.pre_trial_finished_at if version else None,
        detail=(version.pre_trial_error if version else None)
        or "Review of this version's instructions, environment, and verifier. Completion does not mean no defects were found.",
    )
    run_status = (qa.run_status or "not_run").lower()
    execution = DeliveryReviewStage(
        status="failed"
        if qa.run_error
        else "completed"
        if run_status == "success"
        else run_status,
        finished_at=qa.finished_at,
        trial_id=qa.trial_id,
        outdated=qa.status == "outdated",
        detail=qa.run_error or qa.detail,
    )
    verdict = task.verdict if isinstance(task.verdict, dict) else None
    # A withdrawn/failed verdict must never resurrect an old accept/reject label.
    published = task.verdict_status == VerdictStatus.SUCCESS and verdict is not None
    judgment = DeliveryReviewStage(
        status=("accept" if verdict.get("is_good") is True else "reject")
        if published
        else "unavailable",
        outdated=bool(
            published
            and (
                not version
                or verdict_version_id != version.id
                or qa.status not in {"accepted", "needs_fixes"}
            )
        ),
        detail=(verdict.get("reasoning") or verdict.get("primary_issue") or qa.detail)
        if published
        else task.verdict_error or "No current verdict is available.",
    )
    return DeliveryReviews(pre_trial=source, post_trial=execution, verdict=judgment)
