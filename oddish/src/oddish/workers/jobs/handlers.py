"""Per-kind ``JobHandler`` wrappers for the unified ``worker_jobs`` runner.

These are thin adapters: they delegate to the existing
``run_trial_job`` / ``run_analysis_job`` / ``run_verdict_job`` /
``run_task_expand_job`` bodies and translate the resulting domain state
into a ``JobOutcome`` for the runner to record.

Keeping the handlers in one module lets tests monkey-patch the
``get_session`` / ``run_*_job`` module globals without reaching into
the queue execution code.
"""

from __future__ import annotations

from typing import Any

from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    WorkerJobKind,
    get_session,
)
from oddish.workers.jobs.registry import JobOutcome
from oddish.workers.queue.analysis_handler import run_analysis_job
from oddish.workers.queue.task_expand_handler import run_task_expand_job
from oddish.workers.queue.trial_handler import run_trial_job
from oddish.workers.queue.verdict_handler import run_verdict_job


def _fail_retryable(message: str) -> JobOutcome:
    return JobOutcome.fail(message, retryable=True)


def _fail_permanent(message: str) -> JobOutcome:
    return JobOutcome.fail(message, retryable=False)


class TrialJobHandler:
    kind = WorkerJobKind.TRIAL

    def default_queue_key(self, job: Any) -> str:
        return getattr(job, "queue_key", "default") or "default"

    def validate_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    async def run(self, job: Any) -> JobOutcome:
        trial_id = getattr(job, "subject_id", None)
        if not trial_id:
            raise ValueError("TRIAL worker_job missing subject_id")

        await run_trial_job(
            trial_id,
            queue_key=job.queue_key,
            worker_id=getattr(job, "worker_id", None),
            queue_slot=getattr(job, "queue_slot", None),
            modal_function_call_id=getattr(job, "modal_function_call_id", None),
            worker_job_id=getattr(job, "id", None),
        )

        async with get_session() as session:
            trial = await session.get(TrialModel, trial_id)
            if trial is None:
                return _fail_permanent(f"Trial {trial_id} vanished mid-run")
            if trial.status == TrialStatus.SUCCESS:
                return JobOutcome.ok()
            if trial.status == TrialStatus.RETRYING:
                return _fail_retryable(
                    trial.error_message or f"Trial {trial_id} marked RETRYING"
                )
            if trial.status == TrialStatus.FAILED:
                return _fail_retryable(
                    trial.error_message or f"Trial {trial_id} marked FAILED"
                )
            return _fail_retryable(
                f"Trial {trial_id} left in non-terminal status {trial.status!r}"
            )


class AnalysisJobHandler:
    kind = WorkerJobKind.ANALYSIS

    def default_queue_key(self, job: Any) -> str:
        return getattr(job, "queue_key", "analysis") or "analysis"

    def validate_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    async def run(self, job: Any) -> JobOutcome:
        trial_id = getattr(job, "subject_id", None) or (
            (getattr(job, "payload", {}) or {}).get("trial_id")
        )
        if not trial_id:
            raise ValueError(
                "ANALYSIS worker_job missing subject_id / payload.trial_id"
            )

        async with get_session() as session:
            trial = await session.get(TrialModel, trial_id)
            if trial is None:
                return _fail_permanent(f"Trial {trial_id} vanished before analysis")
            if trial.analysis_status in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED):
                trial.analysis_status = AnalysisStatus.QUEUED
                trial.analysis_error = None
                trial.analysis_finished_at = None

        await run_analysis_job(
            trial_id,
            queue_key=job.queue_key,
            modal_function_call_id=getattr(job, "modal_function_call_id", None),
            worker_job_id=getattr(job, "id", None),
        )

        async with get_session() as session:
            trial = await session.get(TrialModel, trial_id)
            if trial is None:
                return _fail_permanent(f"Trial {trial_id} vanished mid-analysis")
            if trial.analysis_status == AnalysisStatus.SUCCESS:
                return JobOutcome.ok()
            if trial.analysis_status == AnalysisStatus.FAILED:
                return _fail_retryable(
                    trial.analysis_error or f"Analysis {trial_id} FAILED"
                )
            return _fail_retryable(
                f"Analysis {trial_id} left in non-terminal status "
                f"{trial.analysis_status!r}"
            )


class VerdictJobHandler:
    kind = WorkerJobKind.VERDICT

    def default_queue_key(self, job: Any) -> str:
        return getattr(job, "queue_key", "verdict") or "verdict"

    def validate_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    async def run(self, job: Any) -> JobOutcome:
        task_id = getattr(job, "subject_id", None) or (
            (getattr(job, "payload", {}) or {}).get("task_id")
        )
        if not task_id:
            raise ValueError("VERDICT worker_job missing subject_id / payload.task_id")

        async with get_session() as session:
            task = await session.get(TaskModel, task_id)
            if task is None:
                return _fail_permanent(f"Task {task_id} vanished before verdict")
            if task.verdict_status in (VerdictStatus.SUCCESS, VerdictStatus.FAILED):
                task.verdict_status = VerdictStatus.QUEUED
                task.verdict_error = None
                task.verdict_finished_at = None

        await run_verdict_job(
            task_id,
            queue_key=job.queue_key,
            modal_function_call_id=getattr(job, "modal_function_call_id", None),
        )

        async with get_session() as session:
            task = await session.get(TaskModel, task_id)
            if task is None:
                return _fail_permanent(f"Task {task_id} vanished mid-verdict")
            if task.verdict_status == VerdictStatus.SUCCESS:
                return JobOutcome.ok()
            if task.verdict_status == VerdictStatus.FAILED:
                return _fail_retryable(
                    task.verdict_error or f"Verdict {task_id} FAILED"
                )
            return _fail_retryable(
                f"Verdict {task_id} left in non-terminal status "
                f"{task.verdict_status!r}"
            )


class TaskExpandJobHandler:
    """Adapter for the ``TASK_EXPAND`` kind.

    Unlike trial / analysis / verdict handlers (which read terminal
    domain state), task expansion reports its outcome directly via the
    ``run_task_expand_job`` return value; any raised exception becomes
    a retryable failure by default.
    """

    kind = WorkerJobKind.TASK_EXPAND

    def default_queue_key(self, job: Any) -> str:
        from oddish.config import settings

        return getattr(job, "queue_key", None) or settings.get_task_expand_queue_key()

    def validate_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload or {})
        if "task_id" not in payload:
            raise ValueError("TASK_EXPAND payload missing task_id")
        if "version" not in payload:
            raise ValueError("TASK_EXPAND payload missing version")
        payload["version"] = int(payload["version"])
        return payload

    async def run(self, job: Any) -> JobOutcome:
        payload = getattr(job, "payload", {}) or {}
        task_id = payload.get("task_id") or getattr(job, "subject_id", None)
        version = payload.get("version")
        if version is None and job.subject_id and "-v" in job.subject_id:
            try:
                version = int(job.subject_id.rsplit("-v", 1)[1])
            except Exception:
                version = None
        if not task_id or version is None:
            raise ValueError("TASK_EXPAND payload missing task_id/version")

        summary = await run_task_expand_job(
            task_id=task_id,
            version=int(version),
            worker_job_id=getattr(job, "id", None),
        )
        return JobOutcome.ok(summary if isinstance(summary, dict) else None)


__all__ = [
    "AnalysisJobHandler",
    "TaskExpandJobHandler",
    "TrialJobHandler",
    "VerdictJobHandler",
]
