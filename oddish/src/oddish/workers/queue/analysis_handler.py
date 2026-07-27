from __future__ import annotations

import asyncio
import shutil
from datetime import timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

from oddish.analyze.models import compute_action_item_id
from oddish.analyze.trajectory_files import parse_trajectory_file_access
from oddish.config import settings
from oddish.core.prompts import resolve_prompt_core
from oddish.db import AnalysisStatus, PromptKind, TaskModel, TaskVersionModel, utcnow
from oddish.db.storage import resolve_task_directory, resolve_trial_directory
from oddish.workers.queue.db_helpers import _trial_session
from oddish.workers.queue.shared import console
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

ANALYSIS_TIMEOUT = 900  # 15 minutes
# Keep comfortably below ``STALE_HEARTBEAT_MINUTES`` (15 min) so a
# slow classification can't drift across the reap threshold mid-run.
# 30s matches the trial heartbeat interval for consistency.
ANALYSIS_HEARTBEAT_INTERVAL_SECONDS = 30
# How long a RUNNING claim is trusted to belong to a live worker. Must exceed
# the worst-case classification (``ANALYSIS_TIMEOUT`` plus the S3 task/trial
# download and sandbox provisioning that precede it); past it the claim is
# presumed abandoned so a worker that died mid-run can't strand the trial.
ANALYSIS_CLAIM_TTL_MINUTES = 30


# The trajectory-summary seam. Building a summary needs the backend-only
# TrajectoryBlock, which oddish/ can't import, so a hosted implementation
# (read-through cache over get_or_generate_summary) is injected here the same
# way pre-trial synth is. Takes a trial_id, returns the summary dict (whose
# ``components`` feed the post-trial classifier) or None when the trial has no
# trajectory. None until registered -- standalone oddish workers classify
# without a component map.
TrajectorySummaryProviderFn = Callable[[str], Awaitable[dict | None]]

_trajectory_summary_provider: TrajectorySummaryProviderFn | None = None


def register_trajectory_summary_provider(fn: TrajectorySummaryProviderFn) -> None:
    """Install the hosted trajectory-summary provider."""
    global _trajectory_summary_provider
    _trajectory_summary_provider = fn


async def _resolve_trajectory_components(trial_id: str) -> list[dict] | None:
    """Ensure a trajectory summary exists and return its component map.

    Best-effort: with no provider registered (standalone oddish) or on any
    generation failure, returns None so classification proceeds exactly as it
    did before components existed. The provider owns its own session and the
    summary LLM call, so this must run outside the trial claim's row lock.
    """
    provider = _trajectory_summary_provider
    if provider is None:
        return None
    try:
        summary = await provider(trial_id)
    except Exception as exc:
        console.print(
            f"[yellow]Trajectory summary unavailable for {trial_id}; classifying "
            f"without a component map: {exc}[/yellow]"
        )
        return None
    if not summary:
        return None
    return summary.get("components") or None


def classification_to_result_dict(classification) -> dict:
    """Render a ``TrialClassification`` for storage on ``trial.analysis``.

    Assigns a stable id to any action item the classifier left id-less,
    mirroring ``build_pre_trial_payload``'s server-side id computation.
    """
    for item in classification.action_items:
        item.id = item.id or compute_action_item_id(item)
    return {
        "trial_name": classification.trial_name,
        "classification": classification.classification.value,
        "subtype": classification.subtype,
        "evidence": classification.evidence,
        "root_cause": classification.root_cause,
        "recommendation": classification.recommendation,
        "reward": classification.reward,
        "action_items": [i.model_dump(mode="json") for i in classification.action_items],
        "exploitation": [e.model_dump(mode="json") for e in classification.exploitation],
    }


async def _heartbeat_analysis_worker_job(
    *,
    worker_job_id: str,
    stop_event: asyncio.Event,
) -> None:
    """Keep ``worker_jobs.heartbeat_at`` fresh during a slow classification.

    Analysis used to run entirely between the claim (which stamps
    ``heartbeat_at``) and the outcome record (which finalizes the
    row). With ``ANALYSIS_TIMEOUT`` = ``STALE_HEARTBEAT_MINUTES`` a
    15-minute-ish classification was within a rounding error of the
    reap threshold. This loop writes every 30s so it can't happen.

    Same failure-tolerance pattern as the trial heartbeat: a DB write
    failure bumps the heartbeat_failure_count / last_heartbeat_error
    breadcrumb for post-mortem, but never crashes the analysis.
    """
    consecutive_failures = 0
    pending_failure_count = 0
    pending_last_error: str | None = None

    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=ANALYSIS_HEARTBEAT_INTERVAL_SECONDS
            )
        except TimeoutError:
            pass

        if stop_event.is_set():
            return

        try:
            await heartbeat_worker_job(
                worker_job_id,
                pending_failure_count=pending_failure_count,
                pending_last_error=pending_last_error,
            )
            if consecutive_failures > 0:
                console.print(
                    f"[green]Analysis worker_job {worker_job_id} heartbeat "
                    f"recovered after {consecutive_failures} failure(s)[/green]"
                )
            consecutive_failures = 0
            pending_failure_count = 0
            pending_last_error = None
        except Exception as exc:
            consecutive_failures += 1
            pending_failure_count += 1
            pending_last_error = f"{type(exc).__name__}: {exc}"


async def classify_trial_and_store(
    trial_id: str,
    should_store: Callable[[Any], Awaitable[bool]] | None = None,
) -> AnalysisStatus | None:
    """Classify one trial and store its analysis.

    Returns ``RUNNING`` when another worker owns a fresh claim -- distinct from
    the ``None`` of an already-terminal trial, because nothing was stored and
    nothing will be until that peer finishes. Callers that aggregate several
    classifications must wait it out rather than proceed on a partial set; see
    ``qa_handler._classify_waiting_out_peer_claim``.
    """
    from oddish.analyze import TrialClassifier

    # Claim the trial. Locked because concurrent QA jobs for one task are
    # routine -- a sweep append enqueues one per batch, and a stale-reaped job
    # is retried alongside the run it duplicated -- so an unlocked read lets
    # several workers claim the same trial and each pay for the identical
    # classification.
    async with _trial_session(trial_id, with_for_update=True) as (session, trial):
        if not trial:
            raise RuntimeError(f"Trial {trial_id} not found in database")
        if trial.deleted_at is not None:
            console.print(f"[dim]Trial {trial_id} was deleted, skipping analysis[/dim]")
            return None

        # Skip if already analyzed
        if trial.analysis_status in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED):
            console.print(
                f"[yellow]Trial {trial_id} already analyzed, skipping[/yellow]"
            )
            return None

        # A fresh RUNNING claim belongs to a live worker; leave it alone. An
        # expired one (or a legacy row with no start stamp) is presumed
        # abandoned and retaken, so a dead worker can't strand the trial.
        claimed_at = trial.analysis_started_at
        if (
            trial.analysis_status == AnalysisStatus.RUNNING
            and claimed_at is not None
            and utcnow() - claimed_at < timedelta(minutes=ANALYSIS_CLAIM_TTL_MINUTES)
        ):
            console.print(
                f"[yellow]Trial {trial_id} is already being classified, skipping[/yellow]"
            )
            return AnalysisStatus.RUNNING

        trial.analysis_status = AnalysisStatus.RUNNING
        trial.analysis_started_at = utcnow()

        # Get task info for downloads
        task = await session.get(TaskModel, trial.task_id)
        if not task:
            raise RuntimeError(f"Task {trial.task_id} not found")

        task_id = task.id
        task_s3_key = task.task_s3_key
        trial_s3_key = trial.trial_s3_key
        task_path = task.task_path
        trial_result_path = trial.harbor_result_path
        trial_agent = trial.agent
        trial_experiment_id = trial.experiment_id
        trial_org_id = trial.org_id
        trial_user_id = trial.billed_user_id
        # Pre-trial findings live on the audited task version; prefer the
        # version this trial ran against, falling back to the task's current
        # version for older trials that predate version stamping.
        pre_trial_items = None
        version_id = trial.task_version_id or task.current_version_id
        if version_id:
            version = await session.get(TaskVersionModel, version_id)
            if version is not None and version.pre_trial:
                pre_trial_items = (version.pre_trial or {}).get("items") or None
        # Probe trials carry the operator directive in harbor_config; their
        # analysis is the shared probe_summary, not the generic classifier.
        trial_harbor_config = trial.harbor_config or {}
        trial_reward = trial.reward

        # The per-trial log auditor is the post-trial QA stage. Resolve its
        # latest immutable registry version while this worker already owns a DB
        # session; local/self-host installs without seeded prompts retain the
        # packaged classifier prompt as a compatibility fallback.
        post_trial_prompt: str | None = None
        post_trial_prompt_version: int | None = None
        post_trial_prompt_id: str | None = None
        post_trial_prompt_scope: str | None = None
        post_trial_prompt_scope_id: str | None = None
        try:
            prompt, prompt_version = await resolve_prompt_core(
                session,
                PromptKind.QA_POST_TRIAL.value,
                org_id=trial_org_id,
                user_id=trial_user_id,
                experiment_id=trial_experiment_id,
                task_id=task_id,
                trial_id=trial_id,
            )
            post_trial_prompt = prompt_version.content
            post_trial_prompt_version = prompt_version.version
            post_trial_prompt_id = prompt.id
            post_trial_prompt_scope = prompt.scope_type or "global"
            post_trial_prompt_scope_id = prompt.scope_id
        except Exception as exc:
            console.print(
                "[yellow]QA_POST_TRIAL prompt unavailable; using packaged "
                f"classifier prompt: {exc}[/yellow]"
            )

        # Log storage locations for debugging
        console.print(f"[dim]Task S3 key: {task_s3_key or '(not set)'}[/dim]")
        console.print(f"[dim]Trial S3 key: {trial_s3_key or '(not set)'}[/dim]")
        console.print(f"[dim]Task local path: {task_path or '(not set)'}[/dim]")
        console.print(
            f"[dim]Trial local path: {trial_result_path or '(not set)'}[/dim]"
        )

    # Resolve task and trial directories (S3 or local)
    temp_task_dir = None
    temp_trial_dir = None
    task_dir_to_use: Path | None = None
    trial_dir_to_use: Path | None = None
    classification_result = None
    analysis_error = None

    try:
        (
            task_dir_to_use,
            temp_task_dir,
            resolved_task_s3_key,
        ) = await resolve_task_directory(
            task_id=task_id,
            task_s3_key=task_s3_key,
            task_path=task_path,
        )
        if temp_task_dir:
            console.print(f"[dim]Downloaded task from S3: {resolved_task_s3_key}[/dim]")
        else:
            console.print(f"[dim]Using local task path: {task_dir_to_use}[/dim]")

        (
            trial_dir_to_use,
            temp_trial_dir,
            resolved_trial_s3_key,
        ) = await resolve_trial_directory(
            trial_id=trial_id,
            trial_s3_key=trial_s3_key,
            trial_result_path=trial_result_path,
        )
        if temp_trial_dir:
            console.print(
                f"[dim]Downloaded trial from S3: {resolved_trial_s3_key}[/dim]"
            )
        else:
            console.print(f"[dim]Using local trial path: {trial_dir_to_use}[/dim]")

        probe_extra_instructions = trial_harbor_config.get("extra_instructions")
        if probe_extra_instructions:
            # Probe trial: produce the same probe_summary the local runner does,
            # via the shared analyzer. Keeps dev and cloud probe analysis in sync.
            from oddish.worker.probe_analysis import (
                extract_probe_artifacts,
                run_probe_analyzer,
            )

            console.print(f"[cyan]Running probe analysis for {trial_id}...[/cyan]")
            artifacts = extract_probe_artifacts(trial_dir_to_use)
            classification_result = await run_probe_analyzer(
                extra_instructions=probe_extra_instructions,
                agent_messages=artifacts["agent_messages"],
                verifier_stdout=artifacts["verifier_stdout"] or "",
                reward=trial_reward,
                result_focus=trial_harbor_config.get("result_focus") or "",
                model=settings.analysis_model,
            )
            console.print(
                f"[green]Probe analysis complete:[/green] {classification_result.get('headline', '')}"
            )
        else:
            # Run classification
            classifier = TrialClassifier(
                model=settings.analysis_model,
                verbose=True,
                timeout=ANALYSIS_TIMEOUT,  # 5 minutes
                prompt_template=post_trial_prompt,
            )

            file_access = [
                fa.__dict__ for fa in parse_trajectory_file_access(trial_dir_to_use)
            ] or None

            # Ensure the trajectory summary exists (generating on miss via the
            # hosted seam) and hand its labeled component map to the classifier.
            # Best-effort: None when unavailable, and never inside the claim lock.
            trajectory_components = await _resolve_trajectory_components(trial_id)

            console.print(f"[cyan]Running classification for {trial_id}...[/cyan]")
            classification = await classifier.classify_trial(
                trial_dir=trial_dir_to_use,
                task_dir=task_dir_to_use,
                trial_agent=trial_agent,
                pre_trial_items=pre_trial_items,
                file_access=file_access,
                trajectory_components=trajectory_components,
                analyzer_block_context={
                    "trial_id": trial_id,
                    "task_id": task_id,
                    "prompt_key": (
                        PromptKind.QA_POST_TRIAL.value
                        if post_trial_prompt_version is not None
                        else None
                    ),
                    "prompt_version": post_trial_prompt_version,
                },
            )
            classification_result = classification_to_result_dict(classification)
            if post_trial_prompt_version is not None:
                classification_result["prompt_kind"] = PromptKind.QA_POST_TRIAL.value
                classification_result["prompt_version"] = post_trial_prompt_version
                classification_result["prompt_id"] = post_trial_prompt_id
                classification_result["prompt_scope"] = post_trial_prompt_scope
                classification_result["prompt_scope_id"] = post_trial_prompt_scope_id

            # Check if classification is a fallback (indicates Claude SDK issue)
            if "classification failed" in (classification.evidence or "").lower():
                console.print(
                    f"[yellow]Classification used fallback for {trial_id}:[/yellow] {classification.evidence}"
                )
            else:
                console.print(
                    f"[green]Classification complete:[/green] {classification.classification.value} - {classification.subtype}"
                )

    except asyncio.CancelledError:
        analysis_error = (
            "Analysis was cancelled by the worker runtime before it finished. "
            "This is usually caused by a worker restart or shutdown."
        )
        console.print(f"[yellow]Analysis cancelled for {trial_id}[/yellow]")
    except Exception as e:
        analysis_error = f"{type(e).__name__}: {e}"
        console.print(f"[red]Analysis error for {trial_id}: {analysis_error}[/red]")
    finally:
        # Clean up temp directories
        if temp_task_dir and temp_task_dir.exists():
            shutil.rmtree(temp_task_dir, ignore_errors=True)
        if temp_trial_dir and temp_trial_dir.exists():
            shutil.rmtree(temp_trial_dir, ignore_errors=True)

    stored_status: AnalysisStatus = AnalysisStatus.FAILED

    async def _store_results() -> None:
        nonlocal stored_status
        async with _trial_session(trial_id, allow_missing=True) as (session, trial):
            if not trial:
                return
            if trial.deleted_at is not None:
                console.print(
                    f"[dim]Analysis {trial_id} ignored; trial was deleted[/dim]"
                )
                return
            if should_store is not None and not await should_store(session):
                console.print(
                    f"[dim]Analysis {trial_id} ignored; owner was cancelled[/dim]"
                )
                return

            if classification_result:
                trial.analysis = classification_result
                trial.analysis_status = AnalysisStatus.SUCCESS
                trial.analysis_finished_at = utcnow()
                trial.analysis_error = None
                stored_status = AnalysisStatus.SUCCESS
                console.print(f"[green]Analysis {trial_id} SUCCESS[/green]")
            else:
                trial.analysis_status = AnalysisStatus.FAILED
                trial.analysis_error = (
                    analysis_error or "Analysis execution failed with exception"
                )
                trial.analysis_finished_at = utcnow()
                stored_status = AnalysisStatus.FAILED
                console.print(f"[red]Analysis {trial_id} FAILED[/red]")

    await asyncio.shield(_store_results())
    return stored_status


async def run_analysis_job(
    trial_id: str,
    queue_key: str,
    modal_function_call_id: str | None = None,
    worker_job_id: str | None = None,
) -> None:
    """Execute analysis for a single claimed trial (legacy per-trial path).

    Task-level QA (``run_task_qa_job``) now classifies every trial in a
    single worker job, so the unified pipeline no longer enqueues
    per-trial ANALYSIS jobs. This handler body is retained so any ANALYSIS
    worker_jobs already in flight across a deploy still run to completion;
    it wraps :func:`classify_trial_and_store` with a heartbeat loop and the
    legacy stage transition.

    1. Download task and trial from S3
    2. Run classification with Claude Code
    3. Store classification in trial.analysis
    4. Advance the (legacy ANALYZING) task toward its QA job
    """
    console.print(
        f"[cyan]Processing analysis[/cyan] {trial_id} (queue_key={queue_key})"
    )
    console.print(f"[dim]Task bucket: {settings.s3_bucket}[/dim]")

    heartbeat_stop = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    if worker_job_id:
        heartbeat_task = asyncio.create_task(
            _heartbeat_analysis_worker_job(
                worker_job_id=worker_job_id,
                stop_event=heartbeat_stop,
            )
        )

    try:
        await classify_trial_and_store(trial_id)
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    async def _advance_stage() -> None:
        from oddish.db import get_session
        from oddish.queue import maybe_advance_legacy_analyzing_task

        async with get_session() as session:
            started = await maybe_advance_legacy_analyzing_task(session, trial_id)
            if started:
                console.print(
                    f"[blue]Task transitioned to VERDICT_PENDING after "
                    f"analysis {trial_id}[/blue]"
                )

    await asyncio.shield(_advance_stage())
