from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import func, select

from oddish.analyze import BaselineValidation, TrialClassification
from oddish.analyze.models import TaskVerdictModel
from oddish.config import settings
from oddish.core.baseline_gate import GATE_SKIP_PREFIX
from oddish.core.verdict_sync import (
    aggregate_exploited_into_pre_trial,
    build_pre_trial_payload,
    build_verdict_payload,
    sync_pre_trial_to_task_version,
    sync_verdict_to_task,
)
from oddish.db import (
    AnalysisStatus,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    WorkerJobModel,
    WorkerJobStatus,
    get_session,
    utcnow,
)
from oddish.workers.queue.analysis_handler import classify_trial_and_store
from oddish.workers.queue.shared import console
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

QA_HEARTBEAT_INTERVAL_SECONDS = 30


async def synthesize_task_verdict(
    classifications: list[TrialClassification],
    baseline: BaselineValidation | None,
    quality_check_passed: bool,
    timeout: float,
) -> TaskVerdictModel:
    """Synthesize the task verdict through VerdictBlock + AnalyzerBlock.

    The block self-provisions the direct API client for ``settings.verdict_model``
    (an OpenAI/Azure model, so the API client uses the OpenAI SDK) with
    TaskVerdictModel as the response format and VERDICT_MAX_TOKENS as the cap,
    and closes it on every exit path. ``timeout`` bounds the whole block run --
    the client has no per-request timeout knob -- falling back to
    VERDICT_TIMEOUT, and the block persists itself to ``analyzer_blocks`` + S3.
    """
    from oddish.analyze.classifier import VERDICT_MAX_TOKENS, VERDICT_TIMEOUT
    from oddish.blocks.analyzer.analyzer_block import (
        AnalyzerBlock,
        AnalyzerInput,
        AnalyzerType,
    )
    from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
    from oddish.blocks.analyzer.verdict.verdict_block import VerdictBlock

    vb = VerdictBlock(
        classifications, baseline=baseline, quality_check_passed=quality_check_passed
    )
    block = AnalyzerBlock(
        analyzer_type=AnalyzerType.TASK_VERDICT,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(input={"num_trials": len(classifications)}),
        # build_prompt() raises rather than sending the degraded placeholder
        # (see VerdictBlock.build_prompt).
        prompt=vb.build_prompt(),
        model=settings.verdict_model,
        max_tokens=VERDICT_MAX_TOKENS,
        response_format=TaskVerdictModel,
        output_transform=vb.to_verdict,
    )
    out = await asyncio.wait_for(block.run(), timeout=timeout or VERDICT_TIMEOUT)
    return TaskVerdictModel(**out.output)


# The pre-trial-synthesis seam: a hosted implementation (AnalyzerBlock-backed)
# can be injected here the same way verdict synthesis is, without oddish
# importing backend/. Mirrors VerdictSynthFn's shape.
# Args: (task_id, task_version_id, trial_ids, timeout).
PreTrialSynthFn = Callable[[str, str, list[str], float], Awaitable[Any]]

# Hosted (AnalyzerBlock-backed) pre-trial synth, registered by backend at import
# via register_pre_trial_synth(). oddish/ can't import backend/, so the sandbox-
# provisioning implementation is injected here rather than imported -- the same
# seam analyzer_llm_client.register_sandbox_client_factory uses. None until
# registered; run_task_qa_job only invokes it when settings.pre_trial_enabled.
_pre_trial_synth_fn: PreTrialSynthFn | None = None


def register_pre_trial_synth(fn: PreTrialSynthFn) -> None:
    """Install the hosted pre-trial-synthesis implementation."""
    global _pre_trial_synth_fn
    _pre_trial_synth_fn = fn


async def _heartbeat_qa_worker_job(
    *,
    worker_job_id: str,
    stop_event: asyncio.Event,
) -> None:
    """Keep ``worker_jobs.heartbeat_at`` fresh during the QA job."""
    consecutive_failures = 0
    pending_failure_count = 0
    pending_last_error: str | None = None

    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=QA_HEARTBEAT_INTERVAL_SECONDS
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
                    f"[green]QA worker_job {worker_job_id} heartbeat "
                    f"recovered after {consecutive_failures} failure(s)[/green]"
                )
            consecutive_failures = 0
            pending_failure_count = 0
            pending_last_error = None
        except Exception as exc:
            consecutive_failures += 1
            pending_failure_count += 1
            pending_last_error = f"{type(exc).__name__}: {exc}"


async def _worker_job_is_running(
    session,
    worker_job_id: str | None,
    *,
    with_for_update: bool = False,
) -> bool:
    if not worker_job_id:
        return True
    statement = select(WorkerJobModel.status).where(WorkerJobModel.id == worker_job_id)
    if with_for_update:
        statement = statement.with_for_update()
    status = await session.scalar(statement)
    return status == WorkerJobStatus.RUNNING


# Lease slack on top of settings.pre_trial_timeout before a RUNNING claim is
# considered abandoned: sandbox provisioning + CLI install happen outside the
# block-run timeout, so the lease must outlive both.
PRE_TRIAL_LEASE_MARGIN_SECONDS = 900


async def _claim_pre_trial_version(task_id: str) -> str | None:
    """Resolve the task's current version and claim it for the pre-trial audit.

    Pre-trial runs once per task *version* (each version is a distinct source
    snapshot), so a sweep-append QA re-run must not re-audit unchanged source:
    a version whose ``pre_trial_status`` is already terminal is skipped. A
    RUNNING claim within its lease is treated as an audit in flight (QA jobs
    are serialized per queue key today; the lease keeps a concurrent worker
    from doubling the sandbox spend if that ever changes), while a RUNNING
    claim past its lease (worker died mid-audit, or a cancelled job never
    released) is reclaimed rather than wedging the version forever. Returns
    None to skip: no version rows (legacy upload), version gone, already
    audited, or an audit in flight.
    """
    async with get_session() as session:
        version_id = await session.scalar(
            select(TaskModel.current_version_id).where(TaskModel.id == task_id)
        )
        if version_id is None:
            return None
        version = await session.get(
            TaskVersionModel, version_id, with_for_update=True
        )
        if version is None:
            return None
        if version.pre_trial_status in (VerdictStatus.SUCCESS, VerdictStatus.FAILED):
            return None
        if (
            version.pre_trial_status == VerdictStatus.RUNNING
            and version.pre_trial_started_at is not None
            and (utcnow() - version.pre_trial_started_at).total_seconds()
            < settings.pre_trial_timeout + PRE_TRIAL_LEASE_MARGIN_SECONDS
        ):
            return None
        version.pre_trial_status = VerdictStatus.RUNNING
        version.pre_trial_started_at = utcnow()
    return str(version_id)


async def _release_pre_trial_claim(task_version_id: str) -> None:
    """Roll a claimed-but-unpersisted audit back to unclaimed so a later QA
    run redoes it promptly (cancelled job, or the task's current version moved
    on mid-audit) instead of waiting out the RUNNING lease. Only a still-
    RUNNING claim is reset; a terminal status written by someone else wins.
    """
    async with get_session() as session:
        version = await session.get(
            TaskVersionModel, task_version_id, with_for_update=True
        )
        if version is not None and version.pre_trial_status == VerdictStatus.RUNNING:
            version.pre_trial_status = None
            version.pre_trial_started_at = None


async def _pre_trial_store_allowed(
    session, worker_job_id: str | None, task_id: str, task_version_id: str
) -> bool:
    """Gate for persisting a pre-trial result: the QA job must still be
    running AND the audited version must still be the task's current version.
    The sandbox agent pulls the task's *current* source, so if a new upload
    landed mid-audit the findings describe the new snapshot, not the claimed
    row -- discard them and let the next QA run audit the new version.
    """
    if not await _worker_job_is_running(session, worker_job_id):
        return False
    current = await session.scalar(
        select(TaskModel.current_version_id).where(TaskModel.id == task_id)
    )
    return current == task_version_id


async def _run_pre_trial_audit(
    task_id: str, worker_job_id: str | None, live_trial_ids: list[str]
) -> None:
    """Claim, run, and persist the per-version pre-trial audit.

    Gated off by default (settings.pre_trial_enabled) and a no-op unless the
    hosted synth is registered; _claim_pre_trial_version keeps it to once per
    task version. Exceptions are swallowed (pre-trial must never fail the
    verdict path) EXCEPT cancellation, which propagates. The ``finally``
    guarantees the release invariant for every exit -- success-but-vetoed
    store, synth failure, a double-fault while recording that failure, and
    job cancellation (CancelledError is a BaseException, so it skips both
    handlers below): a claim that persisted no terminal status is rolled back
    so the next QA run redoes it promptly instead of waiting out the lease.
    """
    pre_trial_version_id: str | None = None
    pre_trial_stored: str | None = None
    try:
        if settings.pre_trial_enabled and _pre_trial_synth_fn is not None:
            pre_trial_version_id = await _claim_pre_trial_version(task_id)
        if pre_trial_version_id is not None:
            pre_trial_items = await _pre_trial_synth_fn(
                task_id,
                pre_trial_version_id,
                live_trial_ids,
                settings.pre_trial_timeout,
            )
            if pre_trial_items is not None:
                pre_trial_stored = await sync_pre_trial_to_task_version(
                    pre_trial_version_id,
                    payload=build_pre_trial_payload(pre_trial_items),
                    error=None,
                    should_store=lambda session: _pre_trial_store_allowed(
                        session, worker_job_id, task_id, pre_trial_version_id
                    ),
                )
    except Exception as exc:  # noqa: BLE001
        console.print(
            f"[red]Pre-trial synthesis failed for {task_id} "
            f"(version {pre_trial_version_id}): "
            f"{type(exc).__name__}: {exc}[/red]"
        )
        # Double-fault guard: recording the failure is itself a DB write, so
        # it can fail too -- caught locally so that can never escape to the
        # caller and get misattributed as a verdict-synthesis failure (which
        # would mark the whole verdict FAILED for what is really just a
        # pre-trial write hiccup).
        try:
            if pre_trial_version_id is not None:
                pre_trial_stored = await sync_pre_trial_to_task_version(
                    pre_trial_version_id,
                    payload=None,
                    error=exc,
                    should_store=lambda session: _pre_trial_store_allowed(
                        session, worker_job_id, task_id, pre_trial_version_id
                    ),
                )
        except Exception as sync_exc:  # noqa: BLE001
            console.print(
                f"[red]Failed to record pre-trial failure for {task_id}: "
                f"{type(sync_exc).__name__}: {sync_exc}[/red]"
            )
    finally:
        if pre_trial_version_id is not None and pre_trial_stored is None:
            try:
                await _release_pre_trial_claim(pre_trial_version_id)
            except Exception as release_exc:  # noqa: BLE001
                # Lease expiry reclaims it eventually; just don't mask the
                # in-flight exception (or cancellation) with a release error.
                console.print(
                    f"[red]Failed to release pre-trial claim "
                    f"{pre_trial_version_id}: "
                    f"{type(release_exc).__name__}: {release_exc}[/red]"
                )


def _trial_needs_classification(analysis_status: AnalysisStatus | None) -> bool:
    """Return true when a live trial still needs QA."""
    return analysis_status not in (AnalysisStatus.SUCCESS, AnalysisStatus.FAILED)


def _classifications_from_trials(trials) -> list:
    """Build verdict inputs from stored trial QA."""
    from oddish.analyze import Classification, TrialClassification
    from oddish.analyze.models import ActionItem, ExploitationAssessment

    classifications: list = []
    for trial in trials:
        analysis = trial.analysis
        if (
            trial.analysis_status == AnalysisStatus.SUCCESS
            and isinstance(analysis, dict)
            and "classification" in analysis
        ):
            classifications.append(
                TrialClassification(
                    trial_name=analysis.get("trial_name", trial.id),
                    classification=Classification(analysis["classification"]),
                    subtype=analysis.get("subtype", "Unknown"),
                    evidence=analysis.get("evidence", ""),
                    root_cause=analysis.get("root_cause", ""),
                    recommendation=analysis.get("recommendation", ""),
                    reward=analysis.get("reward"),
                    action_items=[
                        ActionItem(**x) for x in analysis.get("action_items", [])
                    ],
                    exploitation=[
                        ExploitationAssessment(**x)
                        for x in analysis.get("exploitation", [])
                    ],
                )
            )
    return classifications


async def _load_live_trials_for_classification(
    task_id: str,
) -> list[tuple[str, AnalysisStatus | None]]:
    """Return live trial IDs and QA states.

    Trials created by the Sauron->Oddish bulk migration (``imported_at IS NOT
    NULL``) are excluded: classifying ~1M historical trials was ruled out on
    cost. This deliberately diverges from small ad-hoc ``oddish import`` rows
    (``origin='imported'`` but ``imported_at IS NULL``), which keep the stock
    join-the-QA-job behavior. Migrated trials can still be analyzed manually
    via backfill_analysis.py.
    """
    async with get_session() as session:
        rows = (
            await session.execute(
                select(
                    TrialModel.id,
                    TrialModel.analysis_status,
                ).where(
                    TrialModel.task_id == task_id,
                    TrialModel.superseded_by_trial_id.is_(None),
                    # Exclude bulk-migrated Sauron trials (see docstring): too
                    # costly to classify ~1M historical rows.
                    TrialModel.imported_at.is_(None),
                    # Gate-skipped trials never ran (no logs to classify); a
                    # classifier run on them would emit phantom failures and
                    # pollute the verdict + the agent's pass/fail metrics. New
                    # skipped trials carry status=SKIPPED; the legacy prefix
                    # check still excludes pre-SKIPPED rows (marked FAILED +
                    # the old sentinel message, since we don't backfill).
                    TrialModel.status != TrialStatus.SKIPPED,
                    func.coalesce(TrialModel.error_message, "").notlike(
                        f"{GATE_SKIP_PREFIX}%"
                    ),
                )
            )
        ).all()

    return [(str(trial_id), analysis_status) for trial_id, analysis_status in rows]


async def run_task_qa_job(
    task_id: str,
    queue_key: str,
    modal_function_call_id: str | None = None,
    worker_job_id: str | None = None,
) -> None:
    """Classify a task's live trials and store one verdict."""
    console.print(f"[cyan]Processing task QA[/cyan] {task_id} (queue_key={queue_key})")

    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        if not task:
            raise RuntimeError(f"Task {task_id} not found in database")

        if not await _worker_job_is_running(session, worker_job_id):
            console.print(f"[dim]QA {task_id} skipped; job was cancelled[/dim]")
            return

        if task.verdict_status in (VerdictStatus.SUCCESS, VerdictStatus.FAILED):
            console.print(
                f"[yellow]Task {task_id} verdict already processed, skipping[/yellow]"
            )
            return

        task.verdict_status = VerdictStatus.RUNNING
        task.verdict_started_at = utcnow()

    verdict_result = None
    verdict_error = None
    heartbeat_stop = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    if worker_job_id:
        heartbeat_task = asyncio.create_task(
            _heartbeat_qa_worker_job(
                worker_job_id=worker_job_id,
                stop_event=heartbeat_stop,
            )
        )

    classifications: list[TrialClassification] = []
    try:
        live_trials = await _load_live_trials_for_classification(task_id)

        # Pre-trial: a per-version task-source audit, independent of trial
        # classification -- runs before the per-trial loop, even when there are
        # zero live trials to classify. Failures are swallowed inside so it
        # can never block the verdict path; cancellation propagates.
        await _run_pre_trial_audit(
            task_id, worker_job_id, [trial_id for trial_id, _ in live_trials]
        )

        if not live_trials:
            # Every live trial is excluded from QA (bulk-migrated imports
            # filtered by imported_at; gate-skipped trials). Nothing to classify
            # and no inputs for a verdict -- complete the task cleanly instead of
            # raising "No successful classifications" (which would record a
            # FAILED verdict for what is not an error).
            #
            # maybe_start_qa_stage now refuses to enqueue a QA job with zero
            # eligible trials, so this is the belt-and-braces path for a race
            # (trials excluded between enqueue and run). It MUST leave a
            # TERMINAL verdict_status: QaJobHandler maps SUCCESS -> ok() and
            # everything else (including None) -> retryable failure, so a
            # non-terminal status here would burn every retry and land the job
            # FAILED. verdict stays None: dashboard counts SUCCESS AND
            # verdict->>'is_good' IN (true,false), so a null verdict is counted
            # as neither good nor bad rather than skewing either bucket.
            async with get_session() as session:
                task = await session.get(TaskModel, task_id, with_for_update=True)
                if task and await _worker_job_is_running(session, worker_job_id):
                    task.verdict = None
                    task.verdict_status = VerdictStatus.SUCCESS
                    task.verdict_error = None
                    task.verdict_finished_at = utcnow()
                    task.status = TaskStatus.COMPLETED
                    task.finished_at = utcnow()
            console.print(
                f"[yellow]QA {task_id} skipped: no QA-eligible trials "
                "(all bulk-imported)[/yellow]"
            )
            return
        to_classify = [
            trial_id
            for trial_id, analysis_status in live_trials
            if _trial_needs_classification(analysis_status)
        ]
        if to_classify:
            console.print(
                f"[cyan]Classifying {len(to_classify)} trial(s) for task "
                f"{task_id}...[/cyan]"
            )
        for trial_id in to_classify:
            async with get_session() as session:
                if not await _worker_job_is_running(session, worker_job_id):
                    console.print(
                        f"[dim]QA {task_id} classification stopped; job was cancelled[/dim]"
                    )
                    return
            try:
                await classify_trial_and_store(
                    trial_id,
                    should_store=lambda session: _worker_job_is_running(
                        session, worker_job_id
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                console.print(
                    f"[red]Classification crashed for {trial_id}: "
                    f"{type(exc).__name__}: {exc}[/red]"
                )

        async with get_session() as session:
            if not await _worker_job_is_running(session, worker_job_id):
                console.print(
                    f"[dim]QA {task_id} verdict skipped; job was cancelled[/dim]"
                )
                return
            trials_result = await session.execute(
                select(TrialModel).where(
                    TrialModel.task_id == task_id,
                    TrialModel.superseded_by_trial_id.is_(None),
                )
            )
            trials = trials_result.scalars().all()
            classifications = _classifications_from_trials(trials)

        # The "doubly note" elevation: union each trial's exploitation
        # assessments back onto task.pre_trial. Best-effort -- a failure here
        # is a follow-up-audit gap, not a verdict-blocking error, so it must
        # never stop verdict synthesis from running.
        try:
            await aggregate_exploited_into_pre_trial(task_id)
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[red]Exploited-item aggregation failed for {task_id}: "
                f"{type(exc).__name__}: {exc}[/red]"
            )

        console.print(
            f"[cyan]Computing verdict from {len(classifications)} "
            "classifications...[/cyan]"
        )
        for i, c in enumerate(classifications):
            console.print(
                f"  [{i + 1}] {c.classification.value}: {c.subtype} (reward={c.reward})"
            )

        if not classifications:
            raise ValueError("No successful classifications to synthesize verdict from")

        console.print("[dim]Starting verdict synthesis...[/dim]")
        baseline = None
        quality_check_passed = True
        timeout = 180
        verdict = await synthesize_task_verdict(
            classifications, baseline, quality_check_passed, timeout
        )

        verdict_result = build_verdict_payload(verdict, classifications)

        console.print(
            f"[green]Verdict computed:[/green] {'GOOD' if verdict.is_good else 'NEEDS REVIEW'} "
            f"(confidence: {verdict.confidence})"
        )

    except asyncio.CancelledError:
        verdict_error = (
            "Verdict synthesis was cancelled by the worker runtime before it finished. "
            "This is usually caused by a worker restart or shutdown."
        )
        console.print(f"[yellow]Verdict cancelled for {task_id}[/yellow]")
    except Exception as e:
        verdict_error = f"{type(e).__name__}: {e}"
        console.print(f"[red]Verdict error for {task_id}: {verdict_error}[/red]")
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    status = await asyncio.shield(
        sync_verdict_to_task(
            task_id,
            payload=verdict_result,
            error=verdict_error,
            should_store=lambda session: _worker_job_is_running(session, worker_job_id),
        )
    )
    if status is None:
        console.print(f"[dim]QA {task_id} result ignored; job was cancelled[/dim]")
    elif verdict_result:
        console.print(f"[green]Verdict {task_id} SUCCESS - Task COMPLETED[/green]")
    else:
        console.print(
            f"[yellow]Verdict {task_id} FAILED - Task COMPLETED (no verdict)[/yellow]"
        )
