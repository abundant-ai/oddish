"""Per-kind ``JobHandler`` wrappers for the unified ``worker_jobs`` runner.

These are thin adapters: they delegate to the existing
``run_trial_job`` / ``run_task_expand_job`` / ``run_tag_project_job`` bodies
and translate the resulting domain state into a ``JobOutcome`` for the
runner to record. QA, audits, and analyzer reports run as trials.

Keeping the handlers in one module lets tests monkey-patch the
``get_session`` / ``run_*_job`` module globals without reaching into
the queue execution code.
"""

from __future__ import annotations

from oddish.db import (
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    get_session,
)
from oddish.registry_auth import (
    RegistryAuthDecryptError,
    current_registry_credentials,
    decrypt_credentials,
)
from oddish.workers.jobs.registry import JobOutcome
from oddish.workers.queue.task_expand_handler import run_task_expand_job
from oddish.workers.queue.trial_handler import run_trial_job
from oddish.workers.queue.trial_failures import (
    MODAL_IMAGE_BUILD_FAILED_STAGE,
    is_modal_image_build_failure,
)


class WorkerJobLike:
    id: str
    attempts: int
    queue_key: str
    subject_id: str | None
    payload: dict
    worker_id: str | None
    queue_slot: int | None
    modal_function_call_id: str | None


def _fail_retryable(message: str) -> JobOutcome:
    return JobOutcome.fail(message, retryable=True)


def _fail_permanent(message: str) -> JobOutcome:
    return JobOutcome.fail(message, retryable=False)


class TrialJobHandler:
    kind = WorkerJobKind.TRIAL

    def default_queue_key(self, job: WorkerJobLike) -> str:
        return job.queue_key or "default"

    def validate_payload(self, payload: dict) -> dict:
        return payload

    async def run(self, job: WorkerJobLike) -> JobOutcome:
        trial_id = job.subject_id
        if not trial_id:
            raise ValueError("TRIAL worker_job missing subject_id")

        try:
            creds = decrypt_credentials((job.payload or {}).get("registry_auth_enc"))
        except RegistryAuthDecryptError as exc:
            return _fail_permanent(str(exc))
        cred_token = current_registry_credentials.set(creds or None)
        try:
            await run_trial_job(
                trial_id,
                queue_key=job.queue_key,
                worker_id=job.worker_id,
                queue_slot=job.queue_slot,
                modal_function_call_id=job.modal_function_call_id,
                worker_job_id=job.id,
                worker_job_attempt=job.attempts,
            )
        finally:
            current_registry_credentials.reset(cred_token)

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
                error_message = trial.error_message or f"Trial {trial_id} marked FAILED"
                if getattr(
                    trial, "harbor_stage", None
                ) == MODAL_IMAGE_BUILD_FAILED_STAGE or is_modal_image_build_failure(
                    trial.error_message
                ):
                    return _fail_permanent(error_message)
                return _fail_retryable(error_message)
            return _fail_retryable(
                f"Trial {trial_id} left in non-terminal status {trial.status!r}"
            )


class TaskExpandJobHandler:
    """Adapter for the ``TASK_EXPAND`` kind.

    Unlike trial / analysis / verdict handlers (which read terminal
    domain state), task expansion reports its outcome directly via the
    ``run_task_expand_job`` return value; any raised exception becomes
    a retryable failure by default.
    """

    kind = WorkerJobKind.TASK_EXPAND

    def default_queue_key(self, job: WorkerJobLike) -> str:
        from oddish.config import settings

        return job.queue_key or settings.get_task_expand_queue_key()

    def validate_payload(self, payload: dict) -> dict:
        payload = dict(payload or {})
        if "task_id" not in payload:
            raise ValueError("TASK_EXPAND payload missing task_id")
        if "version" not in payload:
            raise ValueError("TASK_EXPAND payload missing version")
        payload["version"] = int(payload["version"])
        return payload

    async def run(self, job: WorkerJobLike) -> JobOutcome:
        payload = job.payload or {}
        task_id = payload.get("task_id") or job.subject_id
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
            worker_job_id=job.id,
        )
        return JobOutcome.ok(summary if isinstance(summary, dict) else None)


class TagProjectJobHandler:
    """Adapter for the ``TAG_PROJECT`` kind.

    Recompute-from-truth: the handler returns SUCCESS as long as the
    underlying ``run_tag_project_job`` call completes; any raised
    exception becomes a retryable failure (the operation is idempotent).
    """

    kind = WorkerJobKind.TAG_PROJECT

    def default_queue_key(self, job: WorkerJobLike) -> str:
        return job.queue_key or "tag-project"

    def validate_payload(self, payload: dict) -> dict:
        payload = dict(payload or {})
        if not payload.get("scope"):
            raise ValueError("TAG_PROJECT payload missing scope")
        if not payload.get("target_id"):
            raise ValueError("TAG_PROJECT payload missing target_id")
        payload.setdefault("mode", "direct")
        return payload

    async def run(self, job: WorkerJobLike) -> JobOutcome:
        from oddish.workers.queue.tag_project_handler import run_tag_project_job

        summary = await run_tag_project_job(payload=job.payload or {})
        return JobOutcome.ok(summary if isinstance(summary, dict) else None)


__all__ = [
    "TagProjectJobHandler",
    "TaskExpandJobHandler",
    "TrialJobHandler",
]
