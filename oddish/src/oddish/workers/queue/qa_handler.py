from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, not_, select

from oddish.analyze import BaselineValidation, TrialClassification
from oddish.analyze.models import ActionItem, TaskVerdictModel
from oddish.config import settings
from oddish.core.baseline_gate import GATE_SKIP_PREFIX, baseline_agent_clause
from oddish.core.cost_basis import CANCELLED_HARBOR_STAGE
from oddish.core.result_focus_schema import normalize_findings_schema
from oddish.core.verdict_state import (
    abandon_verdict,
    complete_verdict_without_result,
    start_verdict,
)
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
from oddish.workers.queue.analysis_handler import (
    ANALYSIS_CLAIM_TTL_MINUTES,
    classify_trial_and_store,
)
from oddish.workers.queue.provider_failures import is_permanent_provider_failure
from oddish.workers.queue.shared import console
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

QA_HEARTBEAT_INTERVAL_SECONDS = 30
# Re-poll cadence while a peer worker owns a trial's classification claim.
QA_CLAIM_WAIT_POLL_SECONDS = 15
# Backstop on that wait. A claim stops being honored once it passes
# ``ANALYSIS_CLAIM_TTL_MINUTES``, at which point ``classify_trial_and_store``
# retakes it and the poll resolves on its own -- so this only fires if a peer
# keeps renewing a claim forever, and exists purely so the wait can't hang.
QA_CLAIM_WAIT_TIMEOUT_SECONDS = (ANALYSIS_CLAIM_TTL_MINUTES + 5) * 60

# The verdict fallback's structured output. Anthropic accepts only a subset of
# JSON Schema and requires ``additionalProperties: false`` on every object,
# which ``model_json_schema()`` never emits -- the raw Pydantic schema 400s,
# and a 400 is not a permanent provider failure, so the job would retry back
# onto the blocked primary this fallback exists to route around.
VERDICT_FALLBACK_SCHEMA = normalize_findings_schema(
    TaskVerdictModel.model_json_schema()
)


async def _load_pre_trial_items(
    task_id: str, task_version_id: str | None
) -> list[ActionItem] | None:
    """Load audit findings for the exact source snapshot under verdict QA.

    A missing audit is unknown. It must never fall back to findings from an
    older version: those findings describe different task bytes and can turn a
    clean replacement into a false failure. Parsed leniently so one malformed
    current-version item does not hide the rest.
    """
    async with get_session() as session:
        version = (
            await session.get(TaskVersionModel, task_version_id)
            if task_version_id is not None
            else None
        )
        if (
            version is None
            or version.task_id != task_id
            or version.pre_trial_status is None
        ):
            return None
        if version.pre_trial is None:
            return None
        raw_items = version.pre_trial.get("items", [])

    items: list[ActionItem] = []
    for raw in raw_items:
        try:
            items.append(ActionItem.model_validate(raw))
        except Exception:  # noqa: BLE001
            console.print(
                f"[yellow]Skipping a malformed pre_trial item for {task_id}[/yellow]"
            )
    return items


async def synthesize_task_verdict(
    classifications: list[TrialClassification],
    baseline: BaselineValidation | None,
    quality_check_passed: bool,
    timeout: float,
    task_id: str | None = None,
    pre_trial_items: list[ActionItem] | None = None,
    pre_trial_load_failed: bool = False,
) -> TaskVerdictModel:
    """Synthesize the task verdict through VerdictBlock + AnalyzerBlock.

    The block self-provisions the direct API client for ``settings.verdict_model``
    (an OpenAI/Azure model, so the API client uses the OpenAI SDK) with
    TaskVerdictModel as the response format and VERDICT_MAX_TOKENS as the cap,
    and closes it on every exit path. ``timeout`` bounds the whole block run --
    the client has no per-request timeout knob -- falling back to
    VERDICT_TIMEOUT, and the block persists itself to ``analyzer_blocks`` + S3.

    A *permanent* provider failure (see ``provider_failures``) retries once on
    ``settings.verdict_fallback_model``, which is a Claude model and therefore
    a different provider: on 2026-07-26 Azure content-policy blocked the whole
    resource behind ``verdict_model`` and every verdict in prod died for 58
    hours while the Claude-backed blocks in the same worker kept succeeding.
    Transient failures are re-raised untouched -- those are what the worker
    job's own retries are for, and burning the fallback on a timeout would
    silently move verdicts to a different model. If the fallback *also* fails,
    the raised error names both, so the job is classified on the primary's
    permanent failure rather than retried back onto the blocked endpoint.
    """
    import oddish.blocks.analyzer.analyzer_block as ab
    from oddish.analyze.classifier import VERDICT_MAX_TOKENS, VERDICT_TIMEOUT
    from oddish.blocks.analyzer.analyzer_block import AnalyzerInput, AnalyzerType
    from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
    from oddish.blocks.analyzer.verdict.verdict_block import VerdictBlock

    vb = VerdictBlock(
        classifications,
        baseline=baseline,
        quality_check_passed=quality_check_passed,
        pre_trial_items=pre_trial_items,
        pre_trial_load_failed=pre_trial_load_failed,
    )
    prompt = vb.build_prompt()

    async def _run(model: str, *, fallback: bool):
        block = ab.AnalyzerBlock(
            analyzer_type=AnalyzerType.TASK_VERDICT,
            llm_client_type=LLMClientType.API,
            input=AnalyzerInput(input={"num_trials": len(classifications)}),
            task_id=task_id,
            # build_prompt() raises rather than sending the degraded placeholder
            # (see VerdictBlock.build_prompt).
            prompt=prompt,
            model=model,
            max_tokens=VERDICT_MAX_TOKENS,
            # Structured output is provider-specific: response_format on the
            # OpenAI path, a JSON schema on the Anthropic one. Only the arm
            # that matches this model's provider is honored.
            response_format=None if fallback else TaskVerdictModel,
            output_schema=VERDICT_FALLBACK_SCHEMA if fallback else None,
            block_metadata={"verdict_fallback": True} if fallback else None,
            output_transform=vb.to_verdict,
        )
        return await asyncio.wait_for(block.run(), timeout=timeout or VERDICT_TIMEOUT)

    try:
        out = await _run(settings.verdict_model, fallback=False)
    except Exception as exc:
        primary_error = f"{type(exc).__name__}: {exc}"
        if not is_permanent_provider_failure(primary_error):
            raise
        console.print(
            f"[yellow]Verdict model {settings.verdict_model} is permanently "
            f"unavailable ({type(exc).__name__}); retrying on "
            f"{settings.verdict_fallback_model}[/yellow]"
        )
        try:
            out = await _run(settings.verdict_fallback_model, fallback=True)
        except Exception as fallback_exc:
            # Carry the primary's permanent failure into the raised message.
            # It becomes ``task.verdict_error``, which is the only thing
            # QaJobHandler classifies -- and a bare (retryable-looking)
            # fallback error there sends every one of the job's 6 attempts
            # back through the blocked primary first, which is the retry storm
            # this whole path exists to stop.
            raise RuntimeError(
                f"{type(fallback_exc).__name__}: {fallback_exc} "
                f"[verdict fallback {settings.verdict_fallback_model} failed "
                f"after permanent primary failure -- {primary_error}]"
            ) from fallback_exc
    return TaskVerdictModel(**out.output)


@dataclass
class PreTrialSynthResult:
    """What a hosted pre-trial synth returns: the findings plus the spend.

    Spend rides along because ``analysis_costs`` rows carry no block or version
    reference -- a task with several audits has several rows and nothing to tell
    them apart -- so the only place an audit's cost can be attached to its
    version is where the block ran.
    """

    items: list
    cost_usd: float | None = None
    block_id: str | None = None


# The pre-trial-synthesis seam: a hosted implementation (AnalyzerBlock-backed)
# can be injected here the same way verdict synthesis is, without oddish
# importing backend/. Mirrors VerdictSynthFn's shape.
# Args: (task_id, task_version_id, trial_ids, timeout).
PreTrialSynthFn = Callable[[str, str, list[str], float], Awaitable[Any]]

# Hosted (AnalyzerBlock-backed) pre-trial synth, registered by backend at import
# via register_pre_trial_synth(). oddish/ can't import backend/, so the sandbox-
# provisioning implementation is injected here rather than imported -- the same
# seam analyzer_llm_client.register_sandbox_client_factory uses. None until
# registered; the hosted hook resolves its organization-level setting.
_pre_trial_synth_fn: PreTrialSynthFn | None = None


def register_pre_trial_synth(fn: PreTrialSynthFn) -> None:
    """Install the hosted pre-trial-synthesis implementation."""
    global _pre_trial_synth_fn
    _pre_trial_synth_fn = fn


# Optional org-level enablement check (task_id -> enabled), registered by the
# hosted layer alongside the synth. Checked BEFORE claiming a version so a
# disabled org's QA runs don't churn claim/release writes on task_versions.
# The synth itself re-checks and returns None as a backstop.
_pre_trial_enabled_fn: Callable[[str], Awaitable[bool]] | None = None


def register_pre_trial_enabled_check(fn: Callable[[str], Awaitable[bool]]) -> None:
    """Install the hosted org-level pre-trial enablement check."""
    global _pre_trial_enabled_fn
    _pre_trial_enabled_fn = fn


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


async def _classify_waiting_out_peer_claim(
    trial_id: str,
    *,
    task_id: str,
    worker_job_id: str | None,
) -> AnalysisStatus | None:
    """Classify one trial, waiting out a peer worker's claim on it.

    ``classify_trial_and_store`` returns ``RUNNING`` when another worker holds
    a fresh claim. Bailing out of the QA job on that would leave
    ``verdict_status`` non-terminal, and ``QaJobHandler`` treats non-terminal
    as a retryable failure -- whose retry resets an already-terminal verdict
    back to QUEUED before re-running. A peer that finished in the meantime
    would then have its verdict re-synthesized (paying twice) and, if that
    re-run flaked, overwritten with FAILED. Waiting instead keeps this job on
    a terminal-exit-only path.

    The wait self-resolves: once the peer's claim ages past
    ``ANALYSIS_CLAIM_TTL_MINUTES`` the next call retakes it, so a dead peer
    costs one TTL rather than stranding the trial.
    """
    deadline = asyncio.get_running_loop().time() + QA_CLAIM_WAIT_TIMEOUT_SECONDS
    while True:
        status = await classify_trial_and_store(
            trial_id,
            should_store=lambda session: _worker_job_is_running(session, worker_job_id),
        )
        if status != AnalysisStatus.RUNNING:
            return status

        async with get_session() as session:
            if not await _worker_job_is_running(session, worker_job_id):
                console.print(
                    f"[dim]QA {task_id} stopped waiting on {trial_id}; "
                    "job was cancelled[/dim]"
                )
                return status
        if asyncio.get_running_loop().time() >= deadline:
            console.print(
                f"[yellow]QA {task_id} gave up waiting on {trial_id}; "
                "its classification claim outlived the TTL[/yellow]"
            )
            return status

        console.print(
            f"[dim]QA {task_id} waiting on {trial_id}; another worker is "
            "classifying it[/dim]"
        )
        await asyncio.sleep(QA_CLAIM_WAIT_POLL_SECONDS)


# Lease slack on top of settings.pre_trial_timeout before a RUNNING claim is
# considered abandoned: sandbox provisioning + CLI install happen outside the
# block-run timeout, so the lease must outlive both.
PRE_TRIAL_LEASE_MARGIN_SECONDS = 900
# Scheduling and transaction overhead sits outside both the provisioning and
# block timeouts. Keep an additional buffer so a healthy audit cannot be
# reclaimed at the exact sum of those two limits.
PRE_TRIAL_LEASE_JITTER_SECONDS = 60


async def _claim_pre_trial_version(
    task_id: str,
    task_version_id: str | None = None,
    *,
    expected_content_hash: str | None = None,
    enforce_content_hash: bool = False,
) -> tuple[str, str | None, datetime] | None:
    """Claim a task version for the pre-trial audit.

    An audit-only job pins the version the rerun request marked QUEUED and
    passes it here; a full QA run passes None and the claim resolves the
    task's current version. Without the pin, a re-upload between enqueue
    and run would leave the requested version QUEUED forever and audit the
    wrong one.

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
        version_id = task_version_id or await session.scalar(
            select(TaskModel.current_version_id).where(TaskModel.id == task_id)
        )
        if version_id is None:
            return None
        version = await session.get(TaskVersionModel, version_id, with_for_update=True)
        if version is None:
            return None
        if enforce_content_hash and version.content_hash != expected_content_hash:
            return None
        if version.pre_trial_status in (VerdictStatus.SUCCESS, VerdictStatus.FAILED):
            return None
        if (
            version.pre_trial_status == VerdictStatus.RUNNING
            and version.pre_trial_started_at is not None
            and (utcnow() - version.pre_trial_started_at).total_seconds()
            < (
                settings.pre_trial_timeout
                + PRE_TRIAL_LEASE_MARGIN_SECONDS
                + PRE_TRIAL_LEASE_JITTER_SECONDS
            )
        ):
            return None
        claim_started_at = utcnow()
        version.pre_trial_status = VerdictStatus.RUNNING
        version.pre_trial_started_at = claim_started_at
        content_hash = str(version.content_hash) if version.content_hash else None
    return str(version_id), content_hash, claim_started_at


async def _fail_queued_pre_trial_request(
    task_id: str,
    error: str = "Pre-trial audit is not enabled for this org",
    task_version_id: str | None = None,
    *,
    worker_job_id: str | None,
    expected_content_hash: str | None,
) -> None:
    """Terminate an explicit audit request that will not be served.

    The pre-trial audit only runs inside a worker job. When the job skips
    the audit (org has it disabled, no synth registered) or dies, a request
    left QUEUED would never be picked up by anything -- the card would show
    a running audit forever. Only QUEUED is touched: it can only be set by
    an explicit request, so a normal never-audited version stays untouched.
    """
    if worker_job_id is None:
        return
    async with get_session() as session:
        job = await session.get(WorkerJobModel, worker_job_id)
        if job is None or job.status != WorkerJobStatus.RUNNING:
            return
        payload = (job.payload or {}) or {}
        if payload.get("mode") != "pre_trial":
            return
        payload_version_id = payload.get("task_version_id")
        if payload_version_id is not None and payload_version_id != task_version_id:
            return
        has_expected_content_hash = "task_version_content_hash" in payload
        if (
            has_expected_content_hash
            and payload.get("task_version_content_hash") != expected_content_hash
        ):
            return
        version_id = task_version_id or await session.scalar(
            select(TaskModel.current_version_id).where(TaskModel.id == task_id)
        )
        if version_id is None:
            return
        version = await session.get(TaskVersionModel, version_id, with_for_update=True)
        if (
            version is not None
            and (
                not has_expected_content_hash
                or version.content_hash == expected_content_hash
            )
            and version.pre_trial_status == VerdictStatus.QUEUED
        ):
            version.pre_trial_status = VerdictStatus.FAILED
            version.pre_trial_error = error
            version.pre_trial_finished_at = utcnow()


async def run_pre_trial_only_job(
    task_id: str,
    worker_job_id: str | None,
    task_version_id: str | None = None,
    task_version_content_hash: str | None = None,
    enforce_task_version_content_hash: bool = False,
) -> None:
    """Run only the pre-trial audit for one task version.

    Serves the independent audit trigger (``enqueue_pre_trial_worker_job``).
    Trial classifications and the task verdict are not touched. The audit
    writes its result to the version row. If the run raises, a QUEUED
    request is marked FAILED so the card does not show a running audit
    with no job behind it. The version id pins the whole run to the
    version the request marked QUEUED; without it (a job from before the
    field existed) the run falls back to the task's current version.
    """
    console.print(f"[cyan]Processing pre-trial audit[/cyan] {task_id}")
    heartbeat_stop = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    if worker_job_id:
        heartbeat_task = asyncio.create_task(
            _heartbeat_qa_worker_job(
                worker_job_id=worker_job_id,
                stop_event=heartbeat_stop,
            )
        )
    effective_version_id = task_version_id
    try:
        if effective_version_id is None:
            async with get_session() as session:
                resolved_version_id = await session.scalar(
                    select(TaskModel.current_version_id).where(TaskModel.id == task_id)
                )
            effective_version_id = resolved_version_id
        live_trials = await _load_live_trials_for_classification(
            task_id,
            effective_version_id,
        )
        claim = await _run_pre_trial_audit(
            task_id,
            worker_job_id,
            [trial_id for trial_id, _ in live_trials],
            task_version_id=effective_version_id,
            expected_content_hash=task_version_content_hash,
            enforce_content_hash=enforce_task_version_content_hash,
        )
        if claim is not None:
            await _finalize_pre_trial_request(
                task_id,
                task_version_id=claim[0],
                expected_content_hash=claim[1],
                expected_started_at=claim[2],
            )
    except BaseException as exc:
        try:
            await _fail_queued_pre_trial_request(
                task_id,
                error=f"{type(exc).__name__}: {exc}",
                task_version_id=effective_version_id,
                worker_job_id=worker_job_id,
                expected_content_hash=task_version_content_hash,
            )
        except Exception:  # noqa: BLE001
            pass  # best-effort; do not mask the original error
        raise
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            await asyncio.gather(heartbeat_task, return_exceptions=True)


async def _finalize_pre_trial_request(
    task_id: str,
    task_version_id: str | None = None,
    expected_content_hash: str | None = None,
    expected_started_at: datetime | None = None,
) -> None:
    """After an audit-only run, the version must hold a terminal result.

    ``_run_pre_trial_audit`` swallows synth failures, and a vetoed store
    releases the claim back to None. In a full QA run that is fine: the
    next QA run redoes the audit. An explicit re-run has no next run, so
    an unrecorded result would leave the card saying "not audited" with
    no error while the job reports success. Record the failure instead.
    A RUNNING claim is left alone: another worker owns it. A caller without
    a claim token cannot mutate audit state.
    """
    if expected_started_at is None:
        return
    async with get_session() as session:
        version_id = task_version_id or await session.scalar(
            select(TaskModel.current_version_id).where(TaskModel.id == task_id)
        )
        if version_id is None:
            return
        version = await session.get(TaskVersionModel, version_id, with_for_update=True)
        if version is None:
            return
        if version.content_hash != expected_content_hash:
            return
        if (
            version.pre_trial_status == VerdictStatus.RUNNING
            and version.pre_trial_started_at != expected_started_at
        ):
            return
        if version is not None and version.pre_trial_status in (
            None,
            VerdictStatus.PENDING,
            VerdictStatus.QUEUED,
        ):
            version.pre_trial_status = VerdictStatus.FAILED
            version.pre_trial_error = "The audit did not record a result. Run it again."
            version.pre_trial_finished_at = utcnow()


async def _release_pre_trial_claim(
    task_version_id: str,
    *,
    expected_content_hash: str | None,
    expected_started_at: datetime,
) -> None:
    """Roll a claimed-but-unpersisted audit back to unclaimed so a later QA
    run redoes it promptly (cancelled job, or the task's current version moved
    on mid-audit) instead of waiting out the RUNNING lease. Only a still-
    RUNNING claim is reset; a terminal status written by someone else wins.
    """
    async with get_session() as session:
        version = await session.get(
            TaskVersionModel, task_version_id, with_for_update=True
        )
        if (
            version is not None
            and version.pre_trial_status == VerdictStatus.RUNNING
            and version.content_hash == expected_content_hash
            and version.pre_trial_started_at == expected_started_at
        ):
            version.pre_trial_status = None
            version.pre_trial_started_at = None


async def _pre_trial_store_allowed(
    session,
    worker_job_id: str | None,
    task_version_id: str,
    expected_content_hash: str | None,
    expected_started_at: datetime,
) -> bool:
    """Persist only while the QA job and audited source snapshot are current.

    The audit is pinned to a specific, immutable task version, so its findings
    stay valid for that version's row even if a re-upload made a newer version
    current mid-audit. In-place replacement is different: it mutates that same
    version row, so the content hash must still match the snapshot observed
    before synthesis began.
    """
    if not await _worker_job_is_running(session, worker_job_id):
        return False
    version = await session.get(TaskVersionModel, task_version_id)
    return (
        version is not None
        and version.content_hash == expected_content_hash
        and version.pre_trial_status == VerdictStatus.RUNNING
        and version.pre_trial_started_at == expected_started_at
    )


async def _run_pre_trial_audit(
    task_id: str,
    worker_job_id: str | None,
    live_trial_ids: list[str],
    task_version_id: str | None = None,
    *,
    expected_content_hash: str | None = None,
    enforce_content_hash: bool = False,
) -> tuple[str, str | None, datetime] | None:
    """Claim, run, and persist the per-version pre-trial audit.

    A no-op unless the hosted synth is registered; the registered org-level
    enablement check (default: settings.pre_trial_enabled) gates it before any
    claim is taken, and _claim_pre_trial_version keeps it to once per
    task version. Exceptions are swallowed (pre-trial must never fail the
    verdict path) EXCEPT cancellation, which propagates. The ``finally``
    guarantees the release invariant for every exit -- success-but-vetoed
    store, synth failure, a double-fault while recording that failure, and
    job cancellation (CancelledError is a BaseException, so it skips both
    handlers below): a claim that persisted no terminal status is rolled back
    so the next QA run redoes it promptly instead of waiting out the lease.
    """
    pre_trial_version_id: str | None = None
    pre_trial_content_hash: str | None = None
    pre_trial_started_at: datetime | None = None
    pre_trial_stored: str | None = None
    try:
        # settings.pre_trial_enabled is the *default* the registered check falls
        # back to per org, not a master switch: gating on it first made an org
        # opt-in unable to enable anything, which is how prod (default off) ran
        # the audit exactly zero times while the orgs API happily accepted the
        # opt-in. Standalone oddish stays a no-op via _pre_trial_synth_fn.
        synth_fn = _pre_trial_synth_fn
        audit_enabled = synth_fn is not None and (
            await _pre_trial_enabled_fn(task_id)
            if _pre_trial_enabled_fn is not None
            else settings.pre_trial_enabled
        )
        if audit_enabled:
            claim = await _claim_pre_trial_version(
                task_id,
                task_version_id,
                expected_content_hash=expected_content_hash,
                enforce_content_hash=enforce_content_hash,
            )
            if claim is not None:
                (
                    pre_trial_version_id,
                    pre_trial_content_hash,
                    pre_trial_started_at,
                ) = claim
        else:
            # An explicit re-run request (status QUEUED) can only be served
            # here. If the audit is disabled for this org, fail the request
            # with the reason -- leaving it QUEUED would show as running
            # forever with no job behind it.
            await _fail_queued_pre_trial_request(
                task_id,
                task_version_id=task_version_id,
                worker_job_id=worker_job_id,
                expected_content_hash=expected_content_hash,
            )
        if pre_trial_version_id is not None:
            assert pre_trial_started_at is not None
            assert synth_fn is not None
            pre_trial_items = await synth_fn(
                task_id,
                pre_trial_version_id,
                live_trial_ids,
                settings.pre_trial_timeout,
            )
            if pre_trial_items is not None:
                # The hosted synth may return a bare item list (older shape) or
                # a result carrying its block's spend, which is only knowable
                # there -- analysis_costs rows have no version reference.
                items = getattr(pre_trial_items, "items", pre_trial_items)
                pre_trial_stored = await sync_pre_trial_to_task_version(
                    pre_trial_version_id,
                    payload=build_pre_trial_payload(
                        items,
                        cost_usd=getattr(pre_trial_items, "cost_usd", None),
                        block_id=getattr(pre_trial_items, "block_id", None),
                    ),
                    error=None,
                    should_store=lambda session: _pre_trial_store_allowed(
                        session,
                        worker_job_id,
                        pre_trial_version_id,
                        pre_trial_content_hash,
                        pre_trial_started_at,
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
                assert pre_trial_started_at is not None
                pre_trial_stored = await sync_pre_trial_to_task_version(
                    pre_trial_version_id,
                    payload=None,
                    error=exc,
                    should_store=lambda session: _pre_trial_store_allowed(
                        session,
                        worker_job_id,
                        pre_trial_version_id,
                        pre_trial_content_hash,
                        pre_trial_started_at,
                    ),
                )
        except Exception as sync_exc:  # noqa: BLE001
            console.print(
                f"[red]Failed to record pre-trial failure for {task_id}: "
                f"{type(sync_exc).__name__}: {sync_exc}[/red]"
            )
    finally:
        if (
            pre_trial_version_id is not None
            and pre_trial_started_at is not None
            and pre_trial_stored is None
        ):
            try:
                await _release_pre_trial_claim(
                    pre_trial_version_id,
                    expected_content_hash=pre_trial_content_hash,
                    expected_started_at=pre_trial_started_at,
                )
            except Exception as release_exc:  # noqa: BLE001
                # Lease expiry reclaims it eventually; just don't mask the
                # in-flight exception (or cancellation) with a release error.
                console.print(
                    f"[red]Failed to release pre-trial claim "
                    f"{pre_trial_version_id}: "
                    f"{type(release_exc).__name__}: {release_exc}[/red]"
                )
    if pre_trial_version_id is None or pre_trial_started_at is None:
        return None
    return pre_trial_version_id, pre_trial_content_hash, pre_trial_started_at


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
    task_version_id: str | None = None,
) -> list[tuple[str, AnalysisStatus | None]]:
    """Return live trial IDs and QA states for one task version.

    Versioned tasks must never synthesize a current verdict from historical
    trials. ``None`` preserves the legacy behavior for tasks created before
    task versioning existed.

    Trials created by the Sauron->Oddish bulk migration (``imported_at IS NOT
    NULL``) are excluded: classifying ~1M historical trials was ruled out on
    cost. This deliberately diverges from small ad-hoc ``oddish import`` rows
    (``origin='imported'`` but ``imported_at IS NULL``), which keep the stock
    join-the-QA-job behavior. Migrated trials can still be analyzed manually
    via backfill_analysis.py.

    nop/oracle baselines are excluded too: they run fixed scaffolding rather
    than an agent, so there is no trajectory worth the classifier's cost.

    Note this drops the verdict's view of baseline health. ``synthesize_task_verdict``
    is still called with ``baseline=None``, so its ``baseline_summary`` reads
    "Not run", and the classifier's ``task_pre_solved`` / broken-oracle rules no
    longer fire on the trials that would trigger them. The baseline *gate*
    (``evaluate_baseline_gate``) is unaffected -- it reads rewards directly and
    still blocks LLM trials on a faulty task.
    """
    async with get_session() as session:
        rows = (
            await session.execute(
                select(
                    TrialModel.id,
                    TrialModel.analysis_status,
                ).where(
                    TrialModel.task_id == task_id,
                    (
                        TrialModel.task_version_id == task_version_id
                        if task_version_id is not None
                        else True
                    ),
                    TrialModel.superseded_by_trial_id.is_(None),
                    # Exclude bulk-migrated Sauron trials (see docstring): too
                    # costly to classify ~1M historical rows.
                    TrialModel.imported_at.is_(None),
                    # Cancelled trials have no outcome to classify.
                    func.coalesce(TrialModel.harbor_stage, "")
                    != CANCELLED_HARBOR_STAGE,
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
                    # Deterministic baselines: no agent trajectory to classify.
                    not_(baseline_agent_clause(TrialModel.agent)),
                )
            )
        ).all()

    return [(str(trial_id), analysis_status) for trial_id, analysis_status in rows]


async def run_task_qa_job(
    task_id: str,
    queue_key: str,
    modal_function_call_id: str | None = None,
    worker_job_id: str | None = None,
    task_version_id: str | None = None,
    enforce_task_version_id: bool = False,
    task_version_content_hash: str | None = None,
    enforce_task_version_content_hash: bool = False,
) -> bool:
    """Classify a task's live trials and store one verdict.

    Returns true only when a task-version change superseded this pass. The
    worker adapter uses that signal to retire the obsolete job instead of
    retrying it against the replacement version without stage admission.
    """
    console.print(f"[cyan]Processing task QA[/cyan] {task_id} (queue_key={queue_key})")

    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        if not task:
            raise RuntimeError(f"Task {task_id} not found in database")

        if not await _worker_job_is_running(session, worker_job_id):
            console.print(f"[dim]QA {task_id} skipped; job was cancelled[/dim]")
            return False

        if task.verdict_status in (VerdictStatus.SUCCESS, VerdictStatus.FAILED):
            console.print(
                f"[yellow]Task {task_id} verdict already processed, skipping[/yellow]"
            )
            return False

        current_version_id = getattr(task, "current_version_id", None)
        current_version = (
            await session.get(TaskVersionModel, current_version_id)
            if current_version_id is not None
            else None
        )
        current_version_content_hash = (
            current_version.content_hash if current_version is not None else None
        )
        if (enforce_task_version_id and current_version_id != task_version_id) or (
            enforce_task_version_content_hash
            and current_version_content_hash != task_version_content_hash
        ):
            # This queued job was admitted for an older version. Retire it
            # without ever inspecting the replacement version's trials; the
            # worker adapter re-enters current-version admission while keeping
            # this worker as the retry owner.
            abandon_verdict(task)
            task.status = TaskStatus.RUNNING
            task.finished_at = None
            return True
        task_version_id = current_version_id
        task_version_content_hash = current_version_content_hash
        verdict_claim_started_at = utcnow()
        start_verdict(task, now=verdict_claim_started_at)

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
        live_trials = await _load_live_trials_for_classification(
            task_id,
            task_version_id,
        )

        # Pre-trial: a per-version task-source audit, independent of trial
        # classification -- runs before the per-trial loop, even when there are
        # zero live trials to classify. The hosted hooks apply the org-level
        # setting (default: settings.pre_trial_enabled); a no-op unless the
        # synth hook is registered. Failures are swallowed inside so it can
        # never block the verdict path; cancellation propagates.
        await _run_pre_trial_audit(
            task_id,
            worker_job_id,
            [trial_id for trial_id, _ in live_trials],
            task_version_id=task_version_id,
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
                current_version = (
                    await session.get(TaskVersionModel, task_version_id)
                    if task_version_id is not None
                    else None
                )
                current_content_hash = (
                    current_version.content_hash
                    if current_version is not None
                    else None
                )
                source_is_current = bool(
                    task
                    and getattr(task, "current_version_id", None) == task_version_id
                    and current_content_hash == task_version_content_hash
                )
                claim_is_current = bool(
                    source_is_current
                    and task.verdict_started_at == verdict_claim_started_at
                )
                if claim_is_current and await _worker_job_is_running(
                    session, worker_job_id
                ):
                    complete_verdict_without_result(task, now=utcnow())
                    task.status = TaskStatus.COMPLETED
                    task.finished_at = utcnow()
                elif (
                    task
                    and not source_is_current
                    and await _worker_job_is_running(session, worker_job_id)
                ):
                    abandon_verdict(task)
                    task.status = TaskStatus.RUNNING
                    task.finished_at = None
                    return True
            console.print(
                f"[yellow]QA {task_id} skipped: no QA-eligible trials "
                "(all bulk-imported)[/yellow]"
            )
            return False
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
                    return False
            try:
                await _classify_waiting_out_peer_claim(
                    trial_id,
                    task_id=task_id,
                    worker_job_id=worker_job_id,
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
                return False
            trials_result = await session.execute(
                select(TrialModel).where(
                    TrialModel.task_id == task_id,
                    (
                        TrialModel.task_version_id == task_version_id
                        if task_version_id is not None
                        else True
                    ),
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

        # The classify prompt tells trials not to repeat pre-trial findings
        # in their action items, so an audit hole no trial exploited reaches
        # the verdict only through this list. Loaded after the aggregation so
        # the items carry their exploited stamps. Best-effort like above —
        # but a load failure must render as "unknown", never as a clean pass.
        pre_trial_items: list[ActionItem] = []
        pre_trial_load_failed = False
        try:
            loaded = await _load_pre_trial_items(task_id, task_version_id)
            if loaded is None:
                pre_trial_load_failed = True
            else:
                pre_trial_items = loaded
        except Exception as exc:  # noqa: BLE001
            pre_trial_load_failed = True
            console.print(
                f"[red]Pre-trial item load failed for {task_id}: "
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
            classifications,
            baseline,
            quality_check_passed,
            timeout,
            task_id=task_id,
            pre_trial_items=pre_trial_items,
            pre_trial_load_failed=pre_trial_load_failed,
        )

        verdict_result = build_verdict_payload(verdict, classifications)

        console.print(
            f"[green]Verdict computed:[/green] {verdict.verdict.upper()} "
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

    result_superseded = False

    async def _qa_result_is_current(session) -> bool:
        nonlocal result_superseded
        if not await _worker_job_is_running(session, worker_job_id):
            return False
        current_task = await session.get(TaskModel, task_id)
        if current_task is None:
            return False
        current_version = (
            await session.get(TaskVersionModel, task_version_id)
            if task_version_id is not None
            else None
        )
        current_content_hash = (
            current_version.content_hash if current_version is not None else None
        )
        source_is_current = (
            getattr(current_task, "current_version_id", None) == task_version_id
            and current_content_hash == task_version_content_hash
        )
        if source_is_current and (
            current_task.verdict_started_at == verdict_claim_started_at
        ):
            return True
        if source_is_current:
            # Another same-source QA claim owns the verdict generation. This
            # duplicate must not reset or overwrite the owner's RUNNING state.
            return False

        # This QA pass belongs to an obsolete version. Retrying the same worker
        # job would silently re-pin it to the new version without going through
        # maybe_start_qa_stage's current-version trial-completion gates. Reset
        # the task atomically while sync_verdict_to_task still holds its row
        # lock, so a concurrently finishing current-version trial sees RUNNING
        # and can perform normal admission after this transaction commits.
        abandon_verdict(current_task)
        current_task.status = TaskStatus.RUNNING
        current_task.finished_at = None
        result_superseded = True
        return False

    status = await asyncio.shield(
        sync_verdict_to_task(
            task_id,
            payload=verdict_result,
            error=verdict_error,
            should_store=_qa_result_is_current,
        )
    )
    if status is None:
        reason = "task version changed" if result_superseded else "job was cancelled"
        console.print(f"[dim]QA {task_id} result ignored; {reason}[/dim]")
    elif verdict_result:
        console.print(f"[green]Verdict {task_id} SUCCESS - Task COMPLETED[/green]")
    else:
        console.print(
            f"[yellow]Verdict {task_id} FAILED - Task COMPLETED (no verdict)[/yellow]"
        )
    return result_superseded
