"""Analyzer generation worker job: gather trials -> ensure analysis -> run core -> persist.

Thin adapter over oddish.evals.analyzer.core.run_analyzer_eval (pure). Mirrors
qa_handler: sets domain status on the AnalyzerModel; the worker_jobs row status is
written by the dispatcher based on the JobOutcome the handler returns. Also
mirrors qa_handler's heartbeat + reap-safety scaffolding: a long-running analyzer
job must keep ``worker_jobs.heartbeat_at`` fresh (else the reaper flips it to
RETRYING and a second worker re-claims it, double-running the analyzer) and must
check it's still the live owner before writing its terminal state.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from oddish.config import settings, to_anthropic_api_model_id
from oddish.core.analyzer_inputs import (
    build_analyzer_inputs,
    models_by_task_from_rows,
    trial_model_rewards,
)
from oddish.core.analyzer_trial_export import build_trial_analyses_payload
from oddish.core.analyzers import experiment_ids_for_analyzer
from oddish.core.experiment_membership import trial_in_experiment
from oddish.db import get_session
from oddish.db.storage import get_storage_client
from oddish.db.taxonomy_query import load_taxonomy
from oddish.db.models import (
    AnalysisStatus,
    JobStatus,
    AnalyzerModel,
    CapabilityProposalModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    generate_id,
    utcnow,
)
from oddish.evals.analyzer.by_model import build_denominators
from oddish.evals.analyzer.core import run_analyzer_eval
from oddish.evals.analyzer.schemas import AnalyzerEvalConfig, AnalyzerEvalOutput
from oddish.workers.queue.analysis_handler import classify_trial_and_store
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

ANALYZER_HEARTBEAT_INTERVAL_SECONDS = 30

logger = logging.getLogger(__name__)

# The eval strategy seam. Takes rows (not AnalyzerEvalInputs) so a hosted
# implementation can skip build_analyzer_inputs' S3 reads entirely.
EvalRowsFn = Callable[
    [list[tuple[Any, str]], AnalyzerEvalConfig, str], Awaitable[AnalyzerEvalOutput]
]


async def default_eval_rows(
    rows: list[tuple[Any, str]], config: AnalyzerEvalConfig, analyzer_id: str
) -> AnalyzerEvalOutput:
    inputs = await build_analyzer_inputs(rows)
    async with get_session() as session:
        taxonomy = await load_taxonomy(session)
    # Empty findings: this runs before the map step, so counts start at zero
    # and are re-derived with real findings after the eval returns.
    denominators = build_denominators(trial_model_rewards(rows), [])
    return await run_analyzer_eval(
        inputs, config, taxonomy=taxonomy, denominators=denominators
    )


def _trial_analyses_s3_key(analyzer_id: str) -> str:
    return f"analyzers/{analyzer_id}/trial_analyses.json"


async def _maybe_save_trial_analyses(
    *,
    analyzer_id: str,
    findings,
    subanalyses,
    counts,
) -> None:
    """Best-effort upload of the per-trial analyses to S3.

    A failure here logs a warning but must NOT fail the analyzer job — the
    aggregate sections are the primary product. Called only when the analyzer's
    ``save_trial_analyses`` flag is set.
    """
    key = _trial_analyses_s3_key(analyzer_id)
    try:
        payload = build_trial_analyses_payload(
            analyzer_id=analyzer_id,
            findings=findings,
            subanalyses=subanalyses,
            counts=counts,
        )
        data = json.dumps(payload, indent=2).encode("utf-8")
        await get_storage_client().upload_bytes(
            data, key, content_type="application/json"
        )
        logger.info(
            "Saved %d trial analyses for analyzer %s to s3://%s/%s",
            len(payload["trials"]), analyzer_id, settings.s3_bucket, key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to save trial analyses for analyzer %s to s3://%s/%s: %s",
            analyzer_id, settings.s3_bucket, key, exc,
        )


def _build_analyzer_eval_config() -> AnalyzerEvalConfig:
    """Host-side config wiring: pick the model/concurrency from settings/env.

    core.py stays pure (no DB/env reads); this is the one place that
    translates settings into a ``AnalyzerEvalConfig``. Analyzer generation runs
    in-process on the direct Anthropic API (see evals/analyzer/core.py
    ``_default_client``), so the configured Bedrock inference-profile id must
    be converted to its direct-API form -- same convention as
    ``worker/probe_analysis.py``.
    """
    config = AnalyzerEvalConfig()
    analysis_model = to_anthropic_api_model_id(settings.analysis_model) or config.analysis_model

    map_concurrency = config.map_concurrency
    raw_concurrency = os.environ.get("ODDISH_ANALYZER_CONCURRENCY")
    if raw_concurrency is not None:
        map_concurrency = int(raw_concurrency)

    return AnalyzerEvalConfig(
        analysis_model=analysis_model,
        map_concurrency=map_concurrency,
        temperature=config.temperature,
        taxonomy_version=config.taxonomy_version,
        token_budget=config.token_budget,
        prompt_version=config.prompt_version,
        job_kind=WorkerJobKind.ANALYZER.value,
    )


async def _heartbeat_analyzer_worker_job(
    *,
    worker_job_id: str,
    stop_event: asyncio.Event,
) -> None:
    """Keep ``worker_jobs.heartbeat_at`` fresh during the analyzer job."""
    consecutive_failures = 0
    pending_failure_count = 0
    pending_last_error: str | None = None

    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=ANALYZER_HEARTBEAT_INTERVAL_SECONDS
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


def _models_by_task(rows) -> dict[str, list[str]]:
    """task_id -> the distinct models that ran it. Includes trials that passed,
    which is exactly what findings cannot tell us.

    Shared with the sandbox path, which renders the same roster into its reduce
    prompt; the two must not drift.
    """
    return models_by_task_from_rows(rows)


async def _gather_trial_rows(session, analyzer_id: str, org_id: str | None):
    exp_ids = await experiment_ids_for_analyzer(session, analyzer_id)
    if not exp_ids:
        return []
    clauses = [trial_in_experiment(eid) for eid in exp_ids]
    stmt = (
        select(TrialModel, TaskModel.task_path)
        .join(TaskModel, TrialModel.task_id == TaskModel.id)
        .where(
            or_(*clauses),
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.org_id == org_id,
            # Only trials that actually ran count toward num_trials; queued/
            # running/retrying/skipped trials never produced a result to analyzer on.
            TrialModel.status.in_([TrialStatus.SUCCESS, TrialStatus.FAILED]),
        )
    )
    rows = (await session.execute(stmt)).all()
    # De-dupe trials gathered via multiple experiments.
    seen: set[str] = set()
    out = []
    for trial, task_path in rows:
        if trial.id in seen:
            continue
        seen.add(trial.id)
        out.append((trial, task_path))
    return out


async def run_analyzer_generation_job(
    analyzer_id: str,
    *,
    worker_job_id: str | None = None,
    eval_rows_fn: EvalRowsFn = default_eval_rows,
) -> None:
    # 1. Load + set RUNNING.
    async with get_session() as session:
        analyzer = await session.get(AnalyzerModel, analyzer_id, with_for_update=True)
        if analyzer is None:
            return
        if not await _worker_job_is_running(session, worker_job_id):
            return
        if analyzer.status in (JobStatus.SUCCESS, JobStatus.FAILED):
            return
        analyzer.status = JobStatus.RUNNING
        analyzer.started_at = utcnow()
        org_id = analyzer.org_id
        save_trial_analyses = analyzer.save_trial_analyses

    heartbeat_stop = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    if worker_job_id:
        heartbeat_task = asyncio.create_task(
            _heartbeat_analyzer_worker_job(
                worker_job_id=worker_job_id,
                stop_event=heartbeat_stop,
            )
        )

    # Seed terminal state so _store always has something to write. Every exit
    # path from the work block below (success, exception, or cancellation) must
    # reach _store — otherwise the analyzer is stranded in RUNNING with no
    # finished_at/error until a reaper notices. Mirrors qa_handler.
    output = None
    error = None
    try:
        # 2. Ensure each trial has analysis (best-effort), then gather again fresh.
        async with get_session() as session:
            rows = await _gather_trial_rows(session, analyzer_id, org_id)
            trial_ids = [t.id for t, _ in rows]

        for tid in trial_ids:
            if worker_job_id:
                async with get_session() as session:
                    if not await _worker_job_is_running(session, worker_job_id):
                        return
            async with get_session() as session:
                trial = await session.get(TrialModel, tid)
                needs = trial is not None and trial.analysis_status not in (
                    AnalysisStatus.SUCCESS, AnalysisStatus.FAILED
                )
            if needs:
                try:
                    await classify_trial_and_store(
                        tid,
                        should_store=(
                            lambda session: _worker_job_is_running(
                                session, worker_job_id
                            )
                        )
                        if worker_job_id
                        else None,
                    )
                except Exception:
                    pass  # skip un-analyzable trials; they still count toward num_trials

        # 3. Build inputs + run the pure core.
        if worker_job_id:
            async with get_session() as session:
                if not await _worker_job_is_running(session, worker_job_id):
                    return
        async with get_session() as session:
            rows = await _gather_trial_rows(session, analyzer_id, org_id)
        output = await eval_rows_fn(rows, _build_analyzer_eval_config(), analyzer_id)
        if output is not None:
            # Derived host-side from rows, so it holds for every eval strategy --
            # including ones that never build AnalyzerEvalInputs.
            output.models_by_task = _models_by_task(rows)
            if output.by_model is not None:
                # Re-derive with real findings; the pre-eval call above had none.
                output.by_model["denominators"] = build_denominators(
                    trial_model_rewards(rows), output.findings
                )
        if save_trial_analyses and output is not None:
            await _maybe_save_trial_analyses(
                analyzer_id=analyzer_id,
                findings=output.findings,
                subanalyses=output.subanalyses,
                counts=output.counts,
            )
    except asyncio.CancelledError:
        # Reaper/shutdown cancelled us mid-run. Record it as a failure (the
        # _store liveness guard still skips the write if we no longer own the
        # job) instead of leaking a RUNNING row. CancelledError is BaseException,
        # so the broad except below would miss it.
        #
        # `output` is only ever set once run_analyzer_eval has fully returned, so
        # a cancel arriving with it already populated landed in the best-effort
        # trial-analyses upload (whose own `except Exception` cannot catch a
        # BaseException). That side product must never discard a finished
        # analysis -- keep the output so _store still records SUCCESS.
        if output is None:
            error = (
                "Analyzer generation was cancelled by the worker runtime before it "
                "finished (usually a worker restart or shutdown)."
            )
    except Exception as exc:  # noqa: BLE001
        output = None
        error = f"Analyzer generation failed: {exc}"
    finally:
        heartbeat_stop.set()
        if heartbeat_task is not None:
            await asyncio.gather(heartbeat_task, return_exceptions=True)

    # 4. Persist under shield so a cancel mid-write can't corrupt state.
    async def _store() -> None:
        async with get_session() as session:
            if not await _worker_job_is_running(session, worker_job_id):
                return
            analyzer = await session.get(AnalyzerModel, analyzer_id, with_for_update=True)
            if analyzer is None:
                return
            if output is not None:
                analyzer.bad_failure_content = output.sections["bad"]
                analyzer.good_failure_content = output.sections["good"]
                analyzer.universal_capabilities_content = output.sections["capabilities"]
                analyzer.headroom_analysis = output.sections["headroom"]
                analyzer.num_trials = output.counts["trials"]
                analyzer.num_bad_failures = output.counts["bad"]
                analyzer.num_good_failures = output.counts["good"]
                analyzer.breakdown = output.breakdown
                analyzer.reduce_prompt = output.reduce_prompt
                analyzer.findings = [asdict(f) for f in output.findings]
                analyzer.models_by_task = output.models_by_task
                analyzer.taxonomy_version = output.taxonomy_version
                analyzer.taxonomy_snapshot = output.taxonomy_snapshot
                analyzer.by_model = output.by_model
                for p in output.proposals:
                    # Idempotent on (analyzer_id, slug_suggestion): _store runs
                    # under asyncio.shield and a retried job re-enters here.
                    await session.execute(
                        pg_insert(CapabilityProposalModel)
                        .values(
                            id=generate_id(), slug_suggestion=p.slug, name=p.name,
                            description=p.description, example=p.example,
                            category_slugs=p.categories, analyzer_id=analyzer_id,
                            trial_ids=p.trial_ids,
                            trajectory_link=p.trajectory_link, status="PENDING",
                        )
                        .on_conflict_do_nothing(
                            index_elements=["analyzer_id", "slug_suggestion"]
                        )
                    )
                analyzer.status = JobStatus.SUCCESS
                analyzer.error = None
            else:
                analyzer.status = JobStatus.FAILED
                analyzer.error = error
            analyzer.finished_at = utcnow()

    await asyncio.shield(_store())
