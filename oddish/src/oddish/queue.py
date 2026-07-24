from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import (
    ANALYSIS_PIPELINE_QUEUE_KEY,
    NOP_ORACLE_QUEUE_KEY,
    ORPHANED_ANALYSIS_ERROR_PREFIX,
    VERDICT_PIPELINE_QUEUE_KEY,
    is_nop_oracle_agent,
    settings,
)
from oddish.core.baseline_gate import (
    GATE_SKIP_PREFIX,
    GateOutcome,
    evaluate_baseline_gate,
)
from oddish.core.tags.enqueue import enqueue_tag_project_worker_job
from oddish.core.tags.projection import recompute_task_browse_projection
from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    generate_id,
    utcnow,
)
from oddish.db.storage import extract_s3_key_from_path, get_storage_client
from oddish.experiment import generate_experiment_name
from oddish.registry_auth import RegistryCredential, encrypt_credentials
from oddish.schemas import TaskSubmission, TrialSpec
from oddish.task_timeouts import validate_task_timeout_config
from oddish.workers.jobs.enqueue import (
    EnqueueRequest,
    bulk_enqueue_worker_jobs,
    enqueue_worker_job,
)

logger = logging.getLogger(__name__)

USER_CANCELLED_MESSAGE = "Cancelled by user"
CANCELLED_HARBOR_STAGE = "cancelled"


class TrialSupersedeConflict(RuntimeError):
    """A failed attempt changed while a sweep was replacing it."""


ACTIVE_TRIAL_STATUSES = (
    TrialStatus.PENDING,
    TrialStatus.QUEUED,
    TrialStatus.RUNNING,
    TrialStatus.RETRYING,
)
ACTIVE_PIPELINE_STATUSES = (
    AnalysisStatus.PENDING,
    AnalysisStatus.QUEUED,
    AnalysisStatus.RUNNING,
)
ACTIVE_TASK_STATUSES = (
    TaskStatus.PENDING,
    TaskStatus.RUNNING,
    TaskStatus.ANALYZING,
    TaskStatus.VERDICT_PENDING,
)


# =============================================================================
# Task/Trial Cancellation (user-initiated)
# =============================================================================


async def cancel_tasks_runs(
    session: AsyncSession,
    task_ids: list[str],
    org_id: str | None = None,
) -> dict:
    """Cancel in-flight runs for a batch of tasks without deleting data.

    The cancel path walks ``worker_jobs`` (single UPDATE covers trial /
    analysis / verdict kinds uniformly) and then mirrors the terminal
    state back onto the domain rows for live-UI visibility.

    POST-COMMIT CONTRACT: the harvested ``modal_function_call_ids`` and
    ``worker_targets`` are RETURNED, not terminated here -- a rollback must
    never leave live rows pointing at destroyed containers. The caller
    (route or OSS operator invoking this directly) must run
    ``oddish.core.helpers.terminate_run_harvest(result)`` after commit.
    """
    requested_task_ids = list(dict.fromkeys(task_ids))
    if not requested_task_ids:
        return {
            "task_ids": [],
            "not_found_task_ids": [],
            "tasks_found": 0,
            "tasks_cancelled": 0,
            "trials_cancelled": 0,
            "modal_function_call_ids": [],
            "worker_targets": [],
        }

    query = select(TaskModel).where(TaskModel.id.in_(requested_task_ids))
    if org_id:
        query = query.where(TaskModel.org_id == org_id)
    result = await session.execute(query)
    tasks = list(result.scalars().all())
    if not tasks:
        return {"error": "not_found"}

    tasks_by_id = {task.id: task for task in tasks}
    found_task_ids = [
        task_id for task_id in requested_task_ids if task_id in tasks_by_id
    ]
    not_found_task_ids = [
        task_id for task_id in requested_task_ids if task_id not in tasks_by_id
    ]

    # Match worker domain-write order before touching worker_jobs:
    # trial handlers update the trial row, then may lock/update the parent task
    # as they advance task state. Locking tasks first can deadlock against a
    # worker that already holds one of the child trials.
    trial_rows = await session.execute(
        select(TrialModel)
        .where(TrialModel.task_id.in_(found_task_ids))
        .order_by(TrialModel.id)
        .with_for_update()
    )
    trials = list(trial_rows.scalars().all())
    trial_ids = [trial.id for trial in trials]

    locked_task_query = (
        select(TaskModel)
        .where(TaskModel.id.in_(found_task_ids))
        .order_by(TaskModel.id)
        .with_for_update()
    )
    if org_id:
        locked_task_query = locked_task_query.where(TaskModel.org_id == org_id)
    locked_task_rows = await session.execute(locked_task_query)
    tasks = list(locked_task_rows.scalars().all())

    now = utcnow()

    # Cancel every active worker_jobs row belonging to these trials /
    # tasks. One UPDATE, every kind. Returning the canceled rows gives
    # us the Modal function-call ids to terminate remotely and the kind
    # breakdown for the domain-mirror pass below.
    #
    # When the task has no trials yet the trial_ids list is empty; omit the
    # trials branch entirely so we don't pass an empty array to ANY() (asyncpg
    # cannot infer the element type of an empty list) and avoid a spurious
    # FALSE predicate that PostgreSQL still has to plan.
    trial_branch = (
        "OR (subject_table = 'trials' AND subject_id = ANY(:trial_ids))"
        if trial_ids
        else ""
    )
    cancel_sql = text(
        f"""
        WITH to_cancel AS (
            SELECT id,
                   kind::text AS kind,
                   subject_id,
                   modal_function_call_id,
                   provider,
                   external_id
            FROM   worker_jobs
            WHERE  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
              AND  (
                  (subject_table = 'tasks' AND subject_id = ANY(:task_ids))
                  {trial_branch}
              )
            ORDER BY id
            FOR UPDATE
        )
        UPDATE worker_jobs AS w
        SET    status = 'CANCELLED',
               finished_at = NOW(),
               error_message = :cancel_msg,
               current_worker_id = NULL,
               current_queue_slot = NULL,
               modal_function_call_id = NULL,
               payload = w.payload - 'registry_auth_enc'
        FROM   to_cancel
        WHERE  w.id = to_cancel.id
        RETURNING w.id,
                  to_cancel.kind,
                  to_cancel.subject_id,
                  to_cancel.modal_function_call_id,
                  to_cancel.provider,
                  to_cancel.external_id
        """
    )
    cancel_params: dict[str, Any] = {
        "cancel_msg": USER_CANCELLED_MESSAGE,
        "task_ids": found_task_ids,
    }
    if trial_ids:
        cancel_params["trial_ids"] = trial_ids
    canceled_rows = (await session.execute(cancel_sql, cancel_params)).mappings().all()

    modal_fc_ids: list[str] = []
    worker_targets: set[tuple[str, str]] = set()
    canceled_trial_kinds: set[str] = set()
    canceled_verdict_task_ids: set[str] = set()
    canceled_analysis_trial_ids: set[str] = set()

    for row in canceled_rows:
        fc = row.get("modal_function_call_id")
        if fc:
            modal_fc_ids.append(str(fc))
        provider = row.get("provider")
        external_id = row.get("external_id")
        if provider and external_id:
            worker_targets.add((str(provider), str(external_id)))
        kind = row["kind"]
        subject_id = row["subject_id"]
        if kind == "TRIAL" and subject_id:
            canceled_trial_kinds.add(str(subject_id))
        elif kind == "ANALYSIS" and subject_id:
            # Legacy per-trial classification rows, drained across a deploy.
            canceled_analysis_trial_ids.add(str(subject_id))
        elif kind == "QA" and subject_id:
            canceled_verdict_task_ids.add(str(subject_id))

    # Mirror terminal state back to the domain rows so the dashboard
    # sees "FAILED / Cancelled by user" even before handlers exit.
    trials_cancelled = 0
    for trial in trials:
        trial_updated = False
        if trial.id in canceled_trial_kinds or trial.status in ACTIVE_TRIAL_STATUSES:
            # Modal function-call ids now live only on ``worker_jobs``;
            # the ``UPDATE worker_jobs ... RETURNING`` above is the
            # single source for FCs to terminate.
            trial.status = TrialStatus.FAILED
            trial.error_message = USER_CANCELLED_MESSAGE
            trial.finished_at = now
            trial.harbor_stage = CANCELLED_HARBOR_STAGE
            # max_attempts==attempts is the legacy signal the runtime
            # uses to recognise "cancelled by user" during late-arriving
            # Harbor hooks; preserve that contract.
            trial.max_attempts = trial.attempts
            trial.current_worker_id = None
            trial.current_queue_slot = None
            trials_cancelled += 1
            trial_updated = True
        if (
            trial.id in canceled_analysis_trial_ids
            or trial.analysis_status in ACTIVE_PIPELINE_STATUSES
        ):
            trial.analysis_status = AnalysisStatus.FAILED
            trial.analysis_error = USER_CANCELLED_MESSAGE
            trial.analysis_finished_at = now
            trial_updated = True
        if not trial_updated:
            continue

    tasks_cancelled = 0
    for task in tasks:
        task_updated = False
        if task.status in ACTIVE_TASK_STATUSES:
            task.status = TaskStatus.FAILED
            task.finished_at = now
            task_updated = True
        if (
            task.id in canceled_verdict_task_ids
            or task.verdict_status in ACTIVE_PIPELINE_STATUSES
        ):
            task.verdict_status = VerdictStatus.FAILED
            task.verdict_error = USER_CANCELLED_MESSAGE
            task.verdict_finished_at = now
            task_updated = True
        if task_updated:
            tasks_cancelled += 1

    await session.flush()

    return {
        "task_ids": found_task_ids,
        "not_found_task_ids": not_found_task_ids,
        "tasks_found": len(found_task_ids),
        "tasks_cancelled": tasks_cancelled,
        "trials_cancelled": trials_cancelled,
        "modal_function_call_ids": list(dict.fromkeys(modal_fc_ids)),
        "worker_targets": sorted(worker_targets),
    }


# =============================================================================
# Task/Trial Creation
# =============================================================================


async def get_or_create_experiment(
    session: AsyncSession, name: str, org_id: str | None = None
) -> ExperimentModel:
    """Fetch an experiment by name (and org_id if provided) or create it if missing."""
    if org_id:
        query = select(ExperimentModel).where(
            ExperimentModel.org_id == org_id,
            ExperimentModel.name == name,
        )
    else:
        query = select(ExperimentModel).where(ExperimentModel.name == name)

    result = await session.execute(
        query.order_by(ExperimentModel.created_at.desc()).limit(1)
    )
    existing: ExperimentModel | None = result.scalar_one_or_none()
    if existing:
        return existing

    experiment = ExperimentModel(name=name, org_id=org_id)
    session.add(experiment)
    await session.flush()
    return experiment


async def _get_experiment_by_id(
    session: AsyncSession, experiment_id: str, org_id: str | None = None
) -> ExperimentModel | None:
    """Fetch an experiment by ID with optional org scoping."""
    query = select(ExperimentModel).where(ExperimentModel.id == experiment_id)
    if org_id:
        query = query.where(ExperimentModel.org_id == org_id)
    result = await session.execute(query)
    return result.scalar_one_or_none()


async def get_experiment_by_id_or_name(
    session: AsyncSession, experiment_id_or_name: str, org_id: str | None = None
) -> ExperimentModel | None:
    """Fetch an experiment by ID or name with optional org scoping."""
    experiment = await _get_experiment_by_id(session, experiment_id_or_name, org_id)
    if experiment:
        return experiment

    query = select(ExperimentModel).where(ExperimentModel.name == experiment_id_or_name)
    if org_id:
        query = query.where(ExperimentModel.org_id == org_id)
    result = await session.execute(
        query.order_by(ExperimentModel.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


def _derive_task_name(task_path: str, task_id: str | None = None) -> str:
    """Derive a human-readable task name from task_path or task_id."""
    import re

    name = task_path.replace("s3://", "").rstrip("/")

    parts = name.split("/")
    name = parts[-1] if parts else name

    # Skip versioned path segments (e.g. "v1", "v2") produced by
    # resolve_task_storage for the init/complete upload path.
    if re.match(r"^v\d+$", name) and len(parts) > 1:
        name = parts[-2]

    if name == "tasks" and len(parts) > 1:
        name = parts[-2]

    if task_id and name == task_id:
        cleaned = re.sub(r"-[0-9a-f]{8}$", "", name, flags=re.IGNORECASE)
        if cleaned and cleaned != name:
            return cleaned

    return name


# =============================================================================
# Worker-jobs enqueue helpers
# =============================================================================
#
# Every domain-row insertion or stage transition that schedules compute
# work has a sibling ``worker_jobs`` row in the same transaction. The
# dispatcher claims from ``worker_jobs`` only; these helpers are the
# single enqueue surface for the TRIAL and (task-level) QA kinds.
#
# Trajectory analysis is task-scoped: a single QA worker job classifies
# every trial in the task and then synthesizes the task verdict.


def _encrypt_submission_registry_auth(submission: TaskSubmission) -> str | None:
    models = getattr(submission, "registry_auth", None)
    if not models:
        return None
    creds = [
        RegistryCredential(
            username=m.username,
            token=m.token.get_secret_value(),
            registry=m.registry,
        )
        for m in models
    ]
    return encrypt_credentials(creds)


def _trial_job_payload(trial_id: str, registry_auth_enc: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"trial_id": trial_id}
    if registry_auth_enc:
        payload["registry_auth_enc"] = registry_auth_enc
    return payload


async def enqueue_trial_worker_job(
    session: AsyncSession,
    *,
    trial_id: str,
    queue_key: str,
    org_id: str | None,
    max_attempts: int,
    parent_job_id: str | None = None,
    harbor_variant_id: str = "default",
    registry_auth_enc: str | None = None,
) -> WorkerJobModel:
    return await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.TRIAL,
            queue_key=queue_key,
            payload=_trial_job_payload(trial_id, registry_auth_enc),
            subject_table="trials",
            subject_id=trial_id,
            org_id=org_id,
            max_attempts=max_attempts,
            parent_job_id=parent_job_id,
            harbor_variant_id=harbor_variant_id,
        ),
    )


async def requeue_inflight_trial_analysis(
    session: AsyncSession, *, task_id: str, task_version_id: str | None
) -> None:
    """Reopen analyses a dead or cancelled QA attempt left behind.

    RUNNING rows go back to QUEUED (the next QA pass re-classifies anything
    non-terminal), and FAILED rows stamped with the orphaned-analysis sentinel
    (finalized by cleanup while no QA attempt existed) are reopened too --
    they mean "never classified", not "classification ran and failed", so a
    resurrected task must not carry them into its verdict as permanent gaps.

    Rows a QA pass would never classify (superseded, bulk-imported, SKIPPED,
    gate-skipped) are left alone so the orphan sweep's FAILED finalization
    doesn't oscillate with this reset. The id-selection takes its row locks
    with SKIP LOCKED so this never *waits* on a trial row another writer
    holds -- callers may hold the task row lock, and blocking here inverts
    the trials-then-task order ``cancel_tasks_runs`` documents (deadlock).
    Contended rows are healed by the next sweep instead. Raw SQL: the
    soft-delete filter is explicit.
    """
    await session.execute(
        text(
            """
            UPDATE trials
            SET    analysis_status = 'QUEUED',
                   analysis_error = NULL,
                   analysis_finished_at = NULL
            WHERE  id IN (
                SELECT id
                FROM   trials
                WHERE  task_id = :task_id
                  AND  task_version_id IS NOT DISTINCT FROM :task_version_id
                  AND  deleted_at IS NULL
                  AND  superseded_by_trial_id IS NULL
                  AND  imported_at IS NULL
                  AND  status <> 'SKIPPED'
                  AND  COALESCE(error_message, '') NOT LIKE :gate_skip_pattern
                  AND  (
                      analysis_status = 'RUNNING'
                      OR (
                          analysis_status = 'FAILED'
                          AND analysis_error LIKE :orphan_pattern
                      )
                  )
                FOR UPDATE SKIP LOCKED
            )
            """
        ),
        {
            "task_id": task_id,
            "task_version_id": task_version_id,
            "gate_skip_pattern": f"{GATE_SKIP_PREFIX}%",
            "orphan_pattern": f"{ORPHANED_ANALYSIS_ERROR_PREFIX}%",
        },
    )


async def enqueue_qa_worker_job(
    session: AsyncSession,
    *,
    task_id: str,
    org_id: str | None,
    task_version_id: str | None = None,
) -> WorkerJobModel:
    """Enqueue a task QA job, optionally pinned to a task version."""
    payload = {"task_id": task_id}
    if task_version_id is not None:
        payload["task_version_id"] = task_version_id
    return await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.QA,
            queue_key=settings.get_qa_queue_key(),
            payload=payload,
            subject_table="tasks",
            subject_id=task_id,
            org_id=org_id,
        ),
    )


async def enqueue_analyzer_worker_job(
    session: AsyncSession, *, analyzer_id: str, org_id: str | None
) -> WorkerJobModel:
    """Enqueue a cross-experiment analyzer generation job.

    Shares the QA queue key, so it contends with the QA backlog on the claim's
    ``priority DESC, running_count ASC, created_at ASC``. Analyzers are rare,
    interactive (someone is watching the dashboard), and enqueued after the QA
    burst that a sweep produces -- on pure FIFO one waited ~59 minutes. The
    raised priority is the only thing that lets a draining worker pick it up
    ahead of that backlog; every other kind leaves priority at 0.
    """
    return await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.ANALYZER,
            queue_key=settings.get_qa_queue_key(),
            priority=1,
            payload={"analyzer_id": analyzer_id},
            subject_table="analyzers",
            subject_id=analyzer_id,
            org_id=org_id,
        ),
    )


async def enqueue_task_expand_worker_job(
    session: AsyncSession,
    *,
    task_id: str,
    version: int,
    org_id: str | None,
) -> WorkerJobModel:
    """Schedule a task-version expansion.

    Expansion writes each member of the task's tarball as an individual
    S3 object under ``tasks/{task_id}/v{version}-files/`` plus a
    ``.oddish-manifest.json`` sentinel that records the source archive's
    etag. The handler short-circuits when the manifest already matches
    the current archive, so repeat enqueues are cheap.
    """
    return await enqueue_worker_job(
        session,
        EnqueueRequest(
            kind=WorkerJobKind.TASK_EXPAND,
            queue_key=settings.get_task_expand_queue_key(),
            payload={"task_id": task_id, "version": version},
            subject_table="task_versions",
            subject_id=f"{task_id}-v{version}",
            org_id=org_id,
        ),
    )


def _build_harbor_config_for_trial(
    submission: TaskSubmission,
    spec: TrialSpec,
) -> dict[str, Any] | None:
    """Build the harbor_config JSONB payload for a single trial row."""
    base = submission.harbor.model_dump(mode="json", exclude_defaults=True)

    agent_config_payload: dict[str, Any] = {}
    if spec.agent_config:
        agent_config_payload = spec.agent_config.model_dump(
            mode="json", exclude_defaults=True
        )
        agent_config_payload.pop("name", None)
        agent_config_payload.pop("model_name", None)

    if agent_config_payload:
        base["agent_config"] = agent_config_payload

    if submission.extra_instructions:
        base["mode"] = "probe"
        base["extra_instructions"] = submission.extra_instructions
        if submission.probe_name:
            base["probe_name"] = submission.probe_name
        if submission.probe_scope == "experiment":
            base["probe_scope"] = "experiment"

    if submission.result_focus:
        base["result_focus"] = submission.result_focus

    if submission.skill_ids:
        base["skill_ids"] = list(submission.skill_ids)

    return base or None


def _get_next_trial_index(task_id: str, existing_trials: list[TrialModel]) -> int:
    """Return the next numeric suffix for ``{task_id}-{index}`` trial IDs.

    Reruns count toward the index too: every immutable trial -- live
    or superseded -- occupies a slot in the sequence so a freshly
    inserted rerun cannot collide with a row already on disk.
    """
    prefix = f"{task_id}-"
    max_index = -1

    for trial in existing_trials:
        if not trial.id.startswith(prefix):
            continue
        suffix = trial.id[len(prefix) :]
        if suffix.isdigit():
            max_index = max(max_index, int(suffix))

    if max_index >= 0:
        return max_index + 1
    return len(existing_trials)


async def reserve_next_trial_index(session: AsyncSession, *, task_id: str) -> int:
    """SQL-backed sibling of :func:`_get_next_trial_index` for rerun paths.

    The retry path doesn't already have ``task.trials`` loaded and we
    don't want to pull every trial just to compute the suffix. Instead
    we scan ``trials.id`` directly for numeric ``{task_id}-{N}``
    suffixes -- including superseded rows so the new id can never
    collide with an existing prefix in S3.
    """
    prefix = f"{task_id}-"
    rows = await session.execute(
        select(TrialModel.id)
        .where(TrialModel.task_id == task_id)
        .execution_options(include_deleted=True)
    )
    max_index = -1
    for (trial_id,) in rows.all():
        if not isinstance(trial_id, str) or not trial_id.startswith(prefix):
            continue
        suffix = trial_id[len(prefix) :]
        if suffix.isdigit():
            value = int(suffix)
            if value > max_index:
                max_index = value
    return max_index + 1 if max_index >= 0 else 0


# One parameterized INSERT for any number of trials. ``unnest`` keeps the
# statement shape constant regardless of row count -- the path that stays
# cheap under Supavisor transaction pooling + ``statement_cache_size=0``
# (asyncpg 0.31 anonymous statements), unlike a per-N multi-row VALUES.
# Every array parameter is explicitly cast so asyncpg resolves types without
# a round-trip; ``harbor_config`` is passed as text and cast to jsonb (a list
# of dicts is an asyncpg array trap). Columns omitted here (``origin``,
# ``has_trajectory``, ``heartbeat_failure_count``) fall to their DB defaults.
_TRIAL_BULK_INSERT_SQL = text(
    """
    INSERT INTO trials
        (id, name, task_id, task_version_id, experiment_id, org_id,
         billed_user_id, agent, provider, queue_key, model, timeout_minutes,
         environment, harbor_config, harbor_sha, is_probe, max_attempts, status,
         attempts, created_at, updated_at)
    SELECT
        t.id, t.name, t.task_id, t.task_version_id, t.experiment_id, t.org_id,
        t.billed_user_id, t.agent, t.provider, t.queue_key, t.model,
        t.timeout_minutes, t.environment, t.harbor_config::jsonb, t.harbor_sha,
        t.is_probe, t.max_attempts, 'QUEUED'::jobstatus, 0, NOW(), NOW()
    FROM unnest(
        CAST(:id AS text[]),
        CAST(:name AS text[]),
        CAST(:task_id AS text[]),
        CAST(:task_version_id AS text[]),
        CAST(:experiment_id AS text[]),
        CAST(:org_id AS text[]),
        CAST(:agent AS text[]),
        CAST(:provider AS text[]),
        CAST(:queue_key AS text[]),
        CAST(:model AS text[]),
        CAST(:timeout_minutes AS int[]),
        CAST(:environment AS text[]),
        CAST(:harbor_config AS text[]),
        CAST(:harbor_sha AS text[]),
        CAST(:is_probe AS boolean[]),
        CAST(:max_attempts AS int[]),
        CAST(:billed_user_id AS text[])
    ) WITH ORDINALITY AS t(
        id, name, task_id, task_version_id, experiment_id, org_id,
        agent, provider, queue_key, model, timeout_minutes, environment,
        harbor_config, harbor_sha, is_probe, max_attempts, billed_user_id, ord
    )
    """
)


async def _bulk_insert_trials(
    session: AsyncSession, trials: list[dict[str, Any]]
) -> None:
    """Insert many trial rows with a single ``INSERT ... unnest`` statement.

    Equivalent to the old per-row ``session.add(TrialModel(...))`` loop (same
    columns, ``status=QUEUED``), but one statement instead of N. Runs inside
    the caller's session-managed transaction.
    """
    if not trials:
        return
    params = {
        "id": [t["id"] for t in trials],
        "name": [t["name"] for t in trials],
        "task_id": [t["task_id"] for t in trials],
        "task_version_id": [t["task_version_id"] for t in trials],
        "experiment_id": [t["experiment_id"] for t in trials],
        "org_id": [t["org_id"] for t in trials],
        "agent": [t["agent"] for t in trials],
        "provider": [t["provider"] for t in trials],
        "queue_key": [t["queue_key"] for t in trials],
        "model": [t["model"] for t in trials],
        "timeout_minutes": [t["timeout_minutes"] for t in trials],
        "environment": [t["environment"] for t in trials],
        "harbor_config": [
            json.dumps(t["harbor_config"]) if t["harbor_config"] is not None else None
            for t in trials
        ],
        "harbor_sha": [t["harbor_sha"] for t in trials],
        "is_probe": [t["is_probe"] for t in trials],
        "max_attempts": [t["max_attempts"] for t in trials],
        "billed_user_id": [t.get("billed_user_id") for t in trials],
    }
    await session.execute(_TRIAL_BULK_INSERT_SQL, params)


def _submission_gates_llm_trials(submission: TaskSubmission) -> bool:
    """True when this submission should hold its LLM trials on its baselines.

    Active only when the global flag is on, this submission opts in
    (``gate_baselines``, the default), and the submission mixes nop/oracle
    baselines with LLM agents. Applies to both the initial create and later
    appends, so a re-run that adds fresh baselines re-gates its agent trials.
    ``--no-baseline-gate`` sets ``gate_baselines=False`` to run this
    submission's LLM trials ungated (the baselines still run).
    """
    if not settings.gate_llm_on_baselines or not submission.gate_baselines:
        return False
    specs = submission.trials
    has_baseline = any(is_nop_oracle_agent(s.agent) for s in specs)
    has_llm = any(not is_nop_oracle_agent(s.agent) for s in specs)
    return has_baseline and has_llm


def _initial_trial_job_status(agent: str, *, gating: bool) -> WorkerJobStatus:
    """BLOCKED for LLM trials under an armed gate, else QUEUED."""
    if gating and not is_nop_oracle_agent(agent):
        return WorkerJobStatus.BLOCKED
    return WorkerJobStatus.QUEUED


def _ensure_not_collection_target(experiment: "ExperimentModel | None") -> None:
    """Reject runs targeting a read-only collection experiment."""
    if experiment is not None and experiment.is_collection:
        raise ValueError("Cannot run trials into a collection experiment (read-only).")


async def create_task(
    session: AsyncSession,
    submission: TaskSubmission,
    task_id: str | None = None,
    org_id: str | None = None,
    billed_user_id: str | None = None,
) -> TaskModel:
    """Create a task with its trials.

    Trials are created with status=QUEUED which makes them immediately
    visible to the fair-scheduling claim query in workers.

    A ``TaskVersionModel`` (v1) is also created to snapshot the task
    content for this first submission.
    """
    if task_id is None:
        task_id = generate_id()

    task_name = submission.name or _derive_task_name(submission.task_path, task_id)

    task_path = submission.task_path
    task_s3_key = extract_s3_key_from_path(task_path)
    if not task_s3_key:
        local_path = Path(task_path)
        if local_path.exists() and local_path.is_dir():
            validate_task_timeout_config(local_path)
            storage = get_storage_client()
            task_s3_key = await storage.upload_task_directory(task_id, local_path)

    if submission.experiment_id:
        experiment = await get_experiment_by_id_or_name(
            session, submission.experiment_id, org_id
        )
        _ensure_not_collection_target(experiment)
        if not experiment:
            experiment = await get_or_create_experiment(
                session, submission.experiment_id, org_id
            )
    else:
        experiment_name = generate_experiment_name()
        experiment = await get_or_create_experiment(session, experiment_name, org_id)

    # Insert the task first (without version pointer to avoid circular FK).
    task = TaskModel(
        id=task_id,
        name=task_name,
        org_id=org_id,
        user=submission.user or "unknown",
        priority=submission.priority,
        task_path=submission.task_path,
        task_s3_key=task_s3_key,
        tags=submission.tags,
        run_analysis=submission.run_analysis,
        run_probe=submission.run_probe,
        link=submission.link,
    )
    session.add(task)
    await session.flush()

    # Skip the membership-hook browse-projection recompute here: at this
    # point the task has no version yet (created below), so it would run on
    # incomplete pre-version state. The authoritative recompute fires once
    # after the trials are inserted (see end of this function). The append /
    # standalone-link callers keep the hook recompute (their task is already
    # versioned). The TAG_PROJECT enqueue still fires for tag inheritance.
    await _link_task_to_experiment(
        session,
        task_id=task_id,
        experiment_id=experiment.id,
        recompute_browse_projection=False,
    )

    # Determine the version: if one was pre-created during upload, use the
    # latest; otherwise create v1 now that the task row exists.
    existing_max = await session.scalar(
        select(func.max(TaskVersionModel.version)).where(
            TaskVersionModel.task_id == task_id
        )
    )

    if existing_max is not None:
        latest_version_row = (
            await session.execute(
                select(TaskVersionModel).where(
                    TaskVersionModel.task_id == task_id,
                    TaskVersionModel.version == existing_max,
                )
            )
        ).scalar_one()
        version_id = latest_version_row.id
    else:
        version_number = 1
        version_id = f"{task_id}-v{version_number}"
        version_row = TaskVersionModel(
            id=version_id,
            task_id=task_id,
            version=version_number,
            task_path=submission.task_path,
            task_s3_key=task_s3_key,
            content_hash=submission.content_hash,
        )
        session.add(version_row)
        await session.flush()

        if settings.tasks_expand_archive and task_s3_key:
            # Brand-new task created via /tasks/sweep: enqueue the
            # expansion so the drawer's first click hits S3 directly.
            # Re-uploads and registration-only uploads enqueue in
            # ``oddish.core.tasks``; this covers the sweep-creates-v1
            # path they don't exercise.
            await enqueue_task_expand_worker_job(
                session,
                task_id=task_id,
                version=version_number,
                org_id=org_id,
            )

    # Now safe to set the back-pointer and create trials.
    task.current_version_id = version_id

    registry_auth_enc = _encrypt_submission_registry_auth(submission)
    # Gate LLM agents on the baselines when the task mixes both: the LLM trials
    # are enqueued BLOCKED and released (or cancelled) once the nop/oracle
    # baselines finish. Only active when the global flag is on and both kinds
    # are present, so every other submission is unaffected.
    gate_llm_trials = _submission_gates_llm_trials(submission)
    trial_rows: list[dict[str, Any]] = []
    worker_job_requests: list[EnqueueRequest] = []
    for i, spec in enumerate(submission.trials):
        model = settings.normalize_trial_model(spec.agent, spec.model)
        provider = settings.get_provider_for_trial(spec.agent, model)
        queue_key = settings.get_queue_key_for_trial(spec.agent, model)
        trial_id = f"{task_id}-{i}"
        harbor_config = _build_harbor_config_for_trial(submission, spec)
        trial_rows.append(
            {
                "id": trial_id,
                "name": f"{task_name}-{i}",
                "task_id": task_id,
                "task_version_id": version_id,
                "experiment_id": experiment.id,
                "org_id": org_id,
                "billed_user_id": billed_user_id,
                "agent": spec.agent,
                "provider": provider,
                "queue_key": queue_key,
                "model": model,
                "timeout_minutes": spec.timeout_minutes,
                "environment": spec.environment
                or ("modal" if (harbor_config or {}).get("mode") == "probe" else None),
                "harbor_config": harbor_config,
                "is_probe": (harbor_config or {}).get("mode") == "probe",
                "harbor_sha": (harbor_config or {}).get("resolved_sha"),
                "max_attempts": submission.max_trial_attempts,
            }
        )
        worker_job_requests.append(
            EnqueueRequest(
                kind=WorkerJobKind.TRIAL,
                queue_key=queue_key,
                status=_initial_trial_job_status(spec.agent, gating=gate_llm_trials),
                payload=_trial_job_payload(trial_id, registry_auth_enc),
                subject_table="trials",
                subject_id=trial_id,
                org_id=org_id,
                max_attempts=submission.max_trial_attempts,
                harbor_variant_id=(harbor_config or {}).get("variant_id") or "default",
            )
        )

    # Flush the task back-pointer + version before the raw inserts so the
    # trials' task_version_id FK resolves, then bulk-insert in one statement
    # per table.
    await session.flush()
    await _bulk_insert_trials(session, trial_rows)
    await bulk_enqueue_worker_jobs(session, worker_job_requests)

    await session.refresh(task, attribute_names=["trials"])
    await bump_experiment_last_activity(session, experiment_ids=experiment.id)
    await recompute_task_browse_projection(session, task_id=task_id)
    return task


async def _recompute_tag_projection_on_membership_change(
    session,
    *,
    task_id: str,
    experiment_id: str,
    org_id: str | None,
    recompute_browse_projection: bool = True,
) -> None:
    """Invalidation hook for ``_link_task_to_experiment``.

    A task joining (or being restored into) an experiment may inherit one
    or more living EXPERIMENT tags. We:
      * recompute the task's browse-level projection synchronously so the
        UI sees the chip immediately,
      * enqueue a TAG_PROJECT job that recomputes every version of the
        task asynchronously (chunked).

    ``recompute_browse_projection=False`` skips the synchronous recompute --
    used by the create path, which has no version yet here and runs the
    authoritative recompute once after trials are inserted. The async
    TAG_PROJECT enqueue still fires so tag inheritance is unaffected.
    """
    if recompute_browse_projection:
        await recompute_task_browse_projection(session, task_id=task_id)
    await enqueue_tag_project_worker_job(
        session,
        scope="TASK",
        target_id=task_id,
        task_id=task_id,
        org_id=org_id,
        mode="task_all_versions",
    )


async def _recompute_tag_projection_on_membership_removed(
    session,
    *,
    task_id: str,
    experiment_id: str,
    org_id: str | None,
) -> None:
    """Invalidation hook for the un-link path (a task leaving an
    experiment loses any living EXPERIMENT tags it inherited). Mirrors the
    re-link hook: recompute the task's browse projection synchronously and
    enqueue a TAG_PROJECT(task_all_versions) job. The hourly reconciler is
    a backstop.
    """
    await recompute_task_browse_projection(session, task_id=task_id)
    await enqueue_tag_project_worker_job(
        session,
        scope="TASK",
        target_id=task_id,
        task_id=task_id,
        org_id=org_id,
        mode="task_all_versions",
    )


async def _link_task_to_experiment(
    session: AsyncSession,
    *,
    task_id: str,
    experiment_id: str,
    recompute_browse_projection: bool = True,
) -> None:
    """Insert or restore a ``task_experiments`` association row.

    ``recompute_browse_projection`` is forwarded to the membership hook;
    the create path passes ``False`` to avoid a redundant pre-version
    recompute (it runs the authoritative one after inserting trials).
    """
    from oddish.db import task_experiments

    await session.execute(
        pg_insert(task_experiments)
        .values(task_id=task_id, experiment_id=experiment_id)
        .on_conflict_do_update(
            index_elements=["task_id", "experiment_id"],
            set_={"deleted_at": None},
        )
    )
    org_id = await session.scalar(
        text("SELECT org_id FROM tasks WHERE id = :task_id"),
        {"task_id": task_id},
    )
    await _recompute_tag_projection_on_membership_change(
        session,
        task_id=task_id,
        experiment_id=experiment_id,
        org_id=org_id,
        recompute_browse_projection=recompute_browse_projection,
    )


async def bump_experiment_last_activity(
    session: AsyncSession, *, experiment_ids: Any
) -> None:
    """Best-effort refresh of ``experiments.last_activity_at`` to NOW().

    Maintains the denormalized sort key the dashboard "recent experiments"
    query orders on. Failures here MUST NOT block the surrounding write,
    so callers wrap this in try/except (or, equivalently, run it as the
    last step before the surrounding transaction commits).

    ``experiment_ids`` accepts a single id, a list, a set, or any other
    iterable of ids. Empty / falsy values are no-ops.

    Reconciliation: if a write path forgets to call this -- or the call
    races with another write -- the cleanup sweep in
    ``oddish.workers.queue.cleanup`` periodically reconciles drift by
    rederiving the value from ``GREATEST(MAX(tasks.created_at),
    MAX(trials.created_at))``.
    """
    if isinstance(experiment_ids, str):
        ids: list[str] = [experiment_ids]
    else:
        try:
            ids = [str(x) for x in experiment_ids if x]
        except TypeError:
            return
    if not ids:
        return
    try:
        await session.execute(
            text(
                """
                UPDATE experiments
                SET last_activity_at = NOW()
                WHERE id = ANY(:experiment_ids)
                  AND deleted_at IS NULL
                """
            ),
            {"experiment_ids": ids},
        )
    except Exception:  # noqa: BLE001
        # Denormalized maintenance must never block the user's write.
        logger.warning(
            "bump_experiment_last_activity failed for ids=%s", ids, exc_info=True
        )


async def append_trials_to_task(
    session: AsyncSession,
    *,
    task: TaskModel,
    submission: TaskSubmission,
    experiment_id: str | None = None,
    billed_user_id: str | None = None,
    supersede_failed_trial_ids: Sequence[Sequence[str]] | None = None,
) -> list[TrialModel]:
    """Append new queued trials to an existing task.

    New trials are pinned to the task's ``current_version_id``. When
    ``experiment_id`` is given, new trials use that experiment and the
    task is auto-linked to it via ``task_experiments`` (matching the
    implicit behavior of the old single-FK world).

    ``supersede_failed_trial_ids`` is aligned with ``submission.trials``.
    After each replacement row is inserted, the listed failed attempts point
    at it through ``superseded_by_trial_id``. This gives sweep reconciliation
    the same immutable-history behavior as an explicit trial retry while
    keeping the append and supersede writes in one transaction.
    """
    if supersede_failed_trial_ids is None:
        supersede_failed_trial_ids = [() for _ in submission.trials]
    elif len(supersede_failed_trial_ids) != len(submission.trials):
        raise ValueError("supersede_failed_trial_ids must align with submission.trials")
    # ``include_deleted=True`` keeps soft-deleted trials in the suffix
    # search so the next allocated ``{task_id}-{N}`` can never collide
    # with a tombstoned row's primary key.
    trial_rows = await session.execute(
        select(TrialModel)
        .where(TrialModel.task_id == task.id)
        .order_by(TrialModel.created_at.asc(), TrialModel.id.asc())
        .execution_options(include_deleted=True)
    )
    existing_trials = list(trial_rows.scalars().all())
    next_index = _get_next_trial_index(task.id, existing_trials)

    current_version_id = task.current_version_id
    if current_version_id is None:
        current_version_id = await session.scalar(
            select(TaskVersionModel.id)
            .where(TaskVersionModel.task_id == task.id)
            .order_by(TaskVersionModel.version.desc())
            .limit(1)
        )
        if current_version_id is not None:
            task.current_version_id = current_version_id

    # Pick the target experiment: explicit argument wins, otherwise fall back
    # to the first linked experiment (the task's "primary" association).
    if experiment_id is None:
        primary = list(task.experiments or [])
        if not primary:
            raise ValueError(
                f"Task {task.id} has no linked experiments; cannot append trials"
            )
        trial_experiment_id = primary[0].id
    else:
        trial_experiment_id = experiment_id
        await _link_task_to_experiment(
            session, task_id=task.id, experiment_id=experiment_id
        )

    registry_auth_enc = _encrypt_submission_registry_auth(submission)
    new_trial_rows: list[dict[str, Any]] = []
    worker_job_requests: list[EnqueueRequest] = []
    new_trial_ids: list[str] = []
    new_llm_trial_ids: list[str] = []
    supersede_pairs: list[tuple[str, str]] = []
    for spec, superseded_ids in zip(
        submission.trials, supersede_failed_trial_ids, strict=True
    ):
        model = settings.normalize_trial_model(spec.agent, spec.model)
        provider = settings.get_provider_for_trial(spec.agent, model)
        queue_key = settings.get_queue_key_for_trial(spec.agent, model)
        trial_id = f"{task.id}-{next_index}"
        harbor_config = _build_harbor_config_for_trial(submission, spec)
        new_trial_rows.append(
            {
                "id": trial_id,
                "name": f"{task.name}-{next_index}",
                "task_id": task.id,
                "task_version_id": current_version_id,
                "experiment_id": trial_experiment_id,
                "org_id": task.org_id,
                "billed_user_id": billed_user_id,
                "agent": spec.agent,
                "provider": provider,
                "queue_key": queue_key,
                "model": model,
                "timeout_minutes": spec.timeout_minutes,
                "environment": spec.environment
                or ("modal" if (harbor_config or {}).get("mode") == "probe" else None),
                "harbor_config": harbor_config,
                "is_probe": (harbor_config or {}).get("mode") == "probe",
                "harbor_sha": (harbor_config or {}).get("resolved_sha"),
                "max_attempts": submission.max_trial_attempts,
            }
        )
        worker_job_requests.append(
            EnqueueRequest(
                kind=WorkerJobKind.TRIAL,
                queue_key=queue_key,
                payload=_trial_job_payload(trial_id, registry_auth_enc),
                subject_table="trials",
                subject_id=trial_id,
                org_id=task.org_id,
                max_attempts=submission.max_trial_attempts,
                harbor_variant_id=(harbor_config or {}).get("variant_id") or "default",
            )
        )
        new_trial_ids.append(trial_id)
        supersede_pairs.extend((old_id, trial_id) for old_id in superseded_ids)
        if not is_nop_oracle_agent(spec.agent):
            new_llm_trial_ids.append(trial_id)
        next_index += 1

    await _bulk_insert_trials(session, new_trial_rows)
    await bulk_enqueue_worker_jobs(session, worker_job_requests)

    # The replacement rows must exist before the self-referential FK can point
    # old attempts at them. Only live FAILED rows are eligible: if another
    # retry won a race, fail the transaction instead of overwriting its chain.
    for old_trial_id, new_trial_id in supersede_pairs:
        result = await session.execute(
            update(TrialModel)
            .where(
                TrialModel.id == old_trial_id,
                TrialModel.task_id == task.id,
                TrialModel.status == TrialStatus.FAILED,
                TrialModel.superseded_by_trial_id.is_(None),
            )
            .values(superseded_by_trial_id=new_trial_id)
        )
        if cast(Any, result).rowcount != 1:
            raise TrialSupersedeConflict(
                f"Failed trial {old_trial_id} was concurrently superseded or changed"
            )

    # Re-read the inserted rows as ORM objects (in index order) so callers get
    # real ``TrialModel`` instances, matching the old per-row return.
    new_trials: list[TrialModel] = []
    if new_trial_ids:
        fetched = await session.execute(
            select(TrialModel).where(TrialModel.id.in_(new_trial_ids))
        )
        by_id = {t.id: t for t in fetched.scalars().all()}
        new_trials = [by_id[tid] for tid in new_trial_ids]

    if new_trials and task.status in (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.ANALYZING,
        TaskStatus.VERDICT_PENDING,
    ):
        task.status = TaskStatus.RUNNING
        task.finished_at = None

    if new_trials and task.run_analysis:
        task.verdict = None
        task.verdict_status = None
        task.verdict_error = None
        task.verdict_started_at = None
        task.verdict_finished_at = None
        # Cancel any in-flight QA worker_job for this task so a worker
        # that's already claimed (or about to claim) the old row doesn't
        # overwrite the new verdict with stale data. The dispatcher
        # re-enqueues a fresh QA row once all trials for the new set
        # complete.
        await session.execute(
            text(
                """
                UPDATE worker_jobs
                SET    status = 'CANCELLED',
                       finished_at = NOW(),
                       error_message = 'Superseded by appended trials',
                       current_worker_id = NULL,
                       current_queue_slot = NULL,
                       modal_function_call_id = NULL
                WHERE  kind::text = 'QA'
                  AND  subject_table = 'tasks'
                  AND  subject_id = :task_id
                  AND  status::text IN ('QUEUED', 'RETRYING', 'RUNNING', 'BLOCKED')
                """
            ),
            {"task_id": task.id},
        )
        # A classification the cancelled job had mid-flight skips its store
        # (``should_store`` sees the job is no longer RUNNING), which would
        # leave that trial's ``analysis_status`` stuck on RUNNING until the
        # replacement QA job re-claims it. Requeue it now -- and reopen any
        # orphan-finalized FAILED rows, since this append resurrects the task
        # and the fresh QA pass must classify them rather than inherit a
        # permanent gap.
        await requeue_inflight_trial_analysis(
            session,
            task_id=task.id,
            task_version_id=current_version_id,
        )

    # Gate the appended LLM trials on this scope's baselines (the just-added
    # ones and any that already exist), blocking/releasing/cancelling under the
    # task lock. Runs AFTER the reset-to-RUNNING + verdict-reset above so that,
    # when the gate cancels the appended trials, its maybe_start_qa_stage call
    # advances the (now all-terminal) task instead of being clobbered back to
    # RUNNING. An append with no baselines anywhere in scope is left QUEUED.
    # Skipped when this submission opts out (``--no-baseline-gate``): the new
    # LLM trials were enqueued QUEUED and simply run ungated.
    if submission.gate_baselines:
        await apply_baseline_gate_to_new_llm_trials(
            session,
            task_id=task.id,
            task_version_id=current_version_id,
            experiment_id=trial_experiment_id,
            llm_trial_ids=new_llm_trial_ids,
        )

    await session.flush()
    await session.refresh(task, attribute_names=["trials"])
    bump_ids = {trial_experiment_id}
    bump_ids.update(t.experiment_id for t in new_trials if t.experiment_id)
    await bump_experiment_last_activity(session, experiment_ids=bump_ids)
    return new_trials


# =============================================================================
# Stage Transitions
# =============================================================================


async def maybe_start_qa_stage(session: AsyncSession, trial_id: str) -> bool:
    """Transition QA once the selected current-version trials are terminal.

    Historical-version trials do not delay the current-version QA job. They do
    keep the aggregate task status RUNNING until they finish.

    There is only one QA job: it handles both per-trial classification and
    the task verdict, so a task goes straight from RUNNING to
    VERDICT_PENDING (the "QA running" status) rather than passing through a
    separate ANALYZING stage.

    Uses SELECT FOR UPDATE to prevent race conditions.
    """
    trial = await session.get(TrialModel, trial_id)
    if not trial:
        return False

    task_id = trial.task_id

    result = await session.execute(
        select(TaskModel).where(TaskModel.id == task_id).with_for_update()
    )
    task = result.scalar_one_or_none()

    if not task:
        return False

    if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
        return False

    current_version_id = getattr(task, "current_version_id", None)
    if current_version_id is None:
        current_version_id = await session.scalar(
            select(TaskVersionModel.id)
            .where(TaskVersionModel.task_id == task_id)
            .order_by(TaskVersionModel.version.desc())
            .limit(1)
        )
        if current_version_id is None:
            current_version_id = getattr(trial, "task_version_id", None)
        if current_version_id is not None:
            task.current_version_id = current_version_id

    if await session.scalar(
        select(func.count(TrialModel.id)).where(
            and_(
                TrialModel.task_id == task_id,
                TrialModel.task_version_id == current_version_id,
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.status.in_(ACTIVE_TRIAL_STATUSES),
            )
        )
    ):
        return False

    active_trials_query = select(func.count(TrialModel.id)).where(
        TrialModel.task_id == task_id,
        TrialModel.superseded_by_trial_id.is_(None),
        TrialModel.status.in_(ACTIVE_TRIAL_STATUSES),
    )
    qa_eligible_ids: list[str] = []
    if task.run_analysis:
        qa_eligible_ids = sorted(
            str(trial_id)
            for trial_id in await session.scalars(
                select(TrialModel.id).where(
                    TrialModel.task_id == task_id,
                    TrialModel.task_version_id == current_version_id,
                    TrialModel.superseded_by_trial_id.is_(None),
                    TrialModel.imported_at.is_(None),
                    TrialModel.status != TrialStatus.SKIPPED,
                    func.coalesce(TrialModel.error_message, "").notlike(
                        f"{GATE_SKIP_PREFIX}%"
                    ),
                )
            )
        )

    task_verdict = getattr(task, "verdict", None)
    verdict = task_verdict if isinstance(task_verdict, dict) else {}
    if task.verdict_status == VerdictStatus.FAILED:
        remaining_count = await session.scalar(active_trials_query)
        if remaining_count > 0:
            return False
        task.status = (
            TaskStatus.FAILED
            if task.verdict_error == USER_CANCELLED_MESSAGE
            else TaskStatus.COMPLETED
        )
        task.finished_at = utcnow()
        await session.flush()
        return True

    if (
        task.verdict_status == VerdictStatus.SUCCESS
        and verdict.get("task_version_id") == current_version_id
        and verdict.get("trial_ids") == qa_eligible_ids
    ):
        remaining_count = await session.scalar(active_trials_query)
        if remaining_count > 0:
            return False
        task.status = TaskStatus.COMPLETED
        task.finished_at = utcnow()
        await session.flush()
        return True

    # Only enqueue QA when there is actually something to classify. A task can
    # have run_analysis=true yet zero QA-eligible live trials -- e.g. every live
    # trial is a bulk-migrated Sauron import (excluded on cost) or was skipped by
    # the baseline gate (never ran, no logs). Enqueueing then produces a job that
    # can only no-op: run_task_qa_job would leave a non-terminal verdict, which
    # QaJobHandler reads back as a retryable failure -> the job burns all its
    # attempts and lands FAILED for what is not an error. Complete the task
    # instead. Filters MUST mirror qa_handler._load_live_trials_for_classification.
    if qa_eligible_ids:
        task.status = TaskStatus.VERDICT_PENDING
        task.verdict = None
        task.verdict_status = VerdictStatus.QUEUED
        task.verdict_error = None
        task.verdict_started_at = None
        task.verdict_finished_at = None
        await enqueue_qa_worker_job(
            session,
            task_id=task_id,
            org_id=task.org_id,
            task_version_id=current_version_id,
        )
    else:
        remaining_count = await session.scalar(active_trials_query)
        task.status = (
            TaskStatus.RUNNING if remaining_count > 0 else TaskStatus.COMPLETED
        )
        task.finished_at = None if remaining_count > 0 else utcnow()
        # No QA job runs for this task (analysis off, or nothing QA-eligible),
        # so clear any verdict bookkeeping left over from an earlier
        # VERDICT_PENDING pass -- otherwise the task can end COMPLETED while
        # verdict_status still reads QUEUED (e.g. a task bounced back to RUNNING
        # by a late-arriving trial, then completed here with no eligible trials).
        task.verdict_status = None
        task.verdict_error = None

    await session.flush()
    return True


async def maybe_gate_llm_trials(session: AsyncSession, trial_id: str) -> bool:
    """Release or cancel BLOCKED LLM trials once their baselines finish.

    Fires only when *trial_id* is a nop/oracle baseline. The decision is scoped
    to the baseline's **(task version, experiment)**: when every baseline trial
    for that task version in that experiment is terminal, evaluates them and —
    if they validate the task (oracle passes, nop fails) — releases that scope's
    BLOCKED LLM trials to QUEUED; otherwise cancels them and mirrors them to
    FAILED so the task can advance. Scoping by experiment keeps concurrent sweeps
    in different experiments from sharing each other's gate timing or verdict;
    scoping by task version keeps an older version's baselines from validating a
    newer version's (different code) LLM trials.

    A no-op when there are no BLOCKED LLM trials in this scope (the gate was
    never armed) or other baselines are still running. Uses SELECT FOR UPDATE on
    the task row so the "last baseline wins" decision is race-safe.

    Deliberately NOT guarded by ``gate_llm_on_baselines``: arming is flag-gated,
    but *releasing* must always run so that disabling the flag while trials are
    armed can't strand them BLOCKED forever (the dispatcher never claims BLOCKED
    jobs). The cheap "any BLOCKED?" pre-check below keeps it off the hot path.
    """
    trial = await session.get(TrialModel, trial_id)
    if not trial or not is_nop_oracle_agent(trial.agent):
        return False

    return await _resolve_baseline_gate_for_scope(
        session,
        task_id=trial.task_id,
        task_version_id=trial.task_version_id,
        experiment_id=trial.experiment_id,
    )


async def _resolve_baseline_gate_for_scope(
    session: AsyncSession,
    *,
    task_id: str,
    task_version_id: str | None,
    experiment_id: str | None,
) -> bool:
    """Release or cancel a (task version, experiment) scope's BLOCKED LLM trials.

    Locks the task row FOR UPDATE so the decision is serialized against
    concurrent baseline completions *and* new-trial enqueues on the same task.
    No-op (returns False) when the scope has no BLOCKED LLM trials or its
    baselines are still running. When all the scope's baselines are terminal it
    evaluates them: VALID releases the BLOCKED LLM trials to QUEUED, FAULTY
    cancels them (mirrored to FAILED).
    """
    # Cheap, lock-free skip: if the task has no BLOCKED trial jobs at all there
    # is nothing to resolve, so don't take the task lock. This keeps the release
    # path off the hot path (every baseline completion, every reconcile pass)
    # while still running regardless of the feature flag.
    has_blocked = await session.scalar(
        select(WorkerJobModel.id)
        .where(
            and_(
                WorkerJobModel.kind == WorkerJobKind.TRIAL,
                WorkerJobModel.subject_table == "trials",
                WorkerJobModel.status == WorkerJobStatus.BLOCKED,
                WorkerJobModel.subject_id.in_(
                    select(TrialModel.id).where(TrialModel.task_id == task_id)
                ),
            )
        )
        .limit(1)
    )
    if has_blocked is None:
        return False

    locked = await session.scalar(
        select(TaskModel.id).where(TaskModel.id == task_id).with_for_update()
    )
    if locked is None:
        return False

    blocked_trial_ids = (
        (
            await session.execute(
                select(WorkerJobModel.subject_id).where(
                    and_(
                        WorkerJobModel.kind == WorkerJobKind.TRIAL,
                        WorkerJobModel.subject_table == "trials",
                        WorkerJobModel.status == WorkerJobStatus.BLOCKED,
                        WorkerJobModel.subject_id.in_(
                            select(TrialModel.id).where(
                                and_(
                                    TrialModel.task_id == task_id,
                                    TrialModel.experiment_id == experiment_id,
                                    TrialModel.task_version_id == task_version_id,
                                )
                            )
                        ),
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    if not blocked_trial_ids:
        return False

    pending_baselines = await session.scalar(
        select(func.count(TrialModel.id)).where(
            and_(
                TrialModel.task_id == task_id,
                TrialModel.experiment_id == experiment_id,
                TrialModel.task_version_id == task_version_id,
                TrialModel.queue_key == NOP_ORACLE_QUEUE_KEY,
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.status.in_(ACTIVE_TRIAL_STATUSES),
            )
        )
    )
    if pending_baselines:
        return False

    baseline_rows = (
        await session.execute(
            select(TrialModel.agent, TrialModel.reward).where(
                and_(
                    TrialModel.task_id == task_id,
                    TrialModel.experiment_id == experiment_id,
                    TrialModel.task_version_id == task_version_id,
                    TrialModel.queue_key == NOP_ORACLE_QUEUE_KEY,
                    TrialModel.superseded_by_trial_id.is_(None),
                )
            )
        )
    ).all()
    outcome, reason = evaluate_baseline_gate(
        [(agent, reward) for agent, reward in baseline_rows]
    )

    if outcome == GateOutcome.VALID:
        await _unblock_worker_jobs_for_trials(session, list(blocked_trial_ids))
    else:
        await _cancel_gated_llm_trials(session, list(blocked_trial_ids), reason)

    await session.flush()
    return True


async def _unblock_worker_jobs_for_trials(
    session: AsyncSession, trial_ids: list[str]
) -> None:
    """Release BLOCKED trial worker_jobs (BLOCKED -> QUEUED) so they can run."""
    if not trial_ids:
        return
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'QUEUED',
                   available_after = NOW()
            WHERE  subject_table = 'trials'
              AND  kind::text = 'TRIAL'
              AND  subject_id = ANY(:trial_ids)
              AND  status::text = 'BLOCKED'
            """
        ),
        {"trial_ids": trial_ids},
    )


async def _cancel_gated_llm_trials(
    session: AsyncSession, trial_ids: list[str], reason: str
) -> None:
    """Cancel BLOCKED LLM worker_jobs and mark their trials SKIPPED.

    Gated trials never ran, so there is no sandbox to tear down. The trial row
    is marked ``SKIPPED`` (terminal, its own bucket — not a failure) with
    *reason* on it; this is the single place that decides that representation.
    SKIPPED counts as a non-pass toward metrics/done and renders distinctly (⊘).
    """
    if not trial_ids:
        return
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'CANCELLED',
                   finished_at = NOW(),
                   error_message = :reason,
                   current_worker_id = NULL,
                   current_queue_slot = NULL,
                   modal_function_call_id = NULL,
                   payload = payload - 'registry_auth_enc'
            WHERE  subject_table = 'trials'
              AND  kind::text = 'TRIAL'
              AND  subject_id = ANY(:trial_ids)
              AND  status::text = 'BLOCKED'
            """
        ),
        {"trial_ids": trial_ids, "reason": reason},
    )
    await session.execute(
        update(TrialModel)
        .where(
            and_(
                TrialModel.id.in_(trial_ids),
                TrialModel.status.in_(ACTIVE_TRIAL_STATUSES),
            )
        )
        .values(
            status=TrialStatus.SKIPPED,
            error_message=reason,
            finished_at=utcnow(),
            harbor_stage=CANCELLED_HARBOR_STAGE,
            # Terminal now — drop any runtime refs so no worker/slot lingers.
            current_worker_id=None,
            current_queue_slot=None,
        )
    )


async def _scope_has_baseline_trials(
    session: AsyncSession,
    *,
    task_id: str,
    task_version_id: str | None,
    experiment_id: str | None,
) -> bool:
    """True when a (task version, experiment) scope has any live baseline."""
    count = await session.scalar(
        select(func.count(TrialModel.id)).where(
            and_(
                TrialModel.task_id == task_id,
                TrialModel.experiment_id == experiment_id,
                TrialModel.task_version_id == task_version_id,
                TrialModel.queue_key == NOP_ORACLE_QUEUE_KEY,
                TrialModel.superseded_by_trial_id.is_(None),
            )
        )
    )
    return bool(count)


async def _block_worker_jobs_for_trials(
    session: AsyncSession, trial_ids: list[str]
) -> None:
    """Arm the gate on freshly-enqueued trials (QUEUED -> BLOCKED)."""
    if not trial_ids:
        return
    await session.execute(
        text(
            """
            UPDATE worker_jobs
            SET    status = 'BLOCKED'
            WHERE  subject_table = 'trials'
              AND  kind::text = 'TRIAL'
              AND  subject_id = ANY(:trial_ids)
              AND  status::text = 'QUEUED'
            """
        ),
        {"trial_ids": trial_ids},
    )


async def apply_baseline_gate_to_new_llm_trials(
    session: AsyncSession,
    *,
    task_id: str,
    task_version_id: str | None,
    experiment_id: str | None,
    llm_trial_ids: list[str],
) -> None:
    """Gate freshly-enqueued LLM trials on their scope's *existing* baselines.

    The pull-path counterpart to :func:`maybe_gate_llm_trials` (the push path).
    Used by append and retry so an LLM trial added on its own still respects the
    baselines of its (task version, experiment): no baselines in scope leaves it
    QUEUED (ungated); baselines present block it and immediately resolve —
    released if the baselines already validate the task, cancelled if they are
    faulty, or left BLOCKED for the push path if baselines are still running.

    Holds the task row FOR UPDATE across the check + block + resolve so it can't
    interleave with a concurrent baseline completion (which locks the same row),
    closing the "blocked just after the last baseline finished" race.
    """
    if not settings.gate_llm_on_baselines or not llm_trial_ids:
        return

    locked = await session.scalar(
        select(TaskModel.id).where(TaskModel.id == task_id).with_for_update()
    )
    if locked is None:
        return

    if not await _scope_has_baseline_trials(
        session,
        task_id=task_id,
        task_version_id=task_version_id,
        experiment_id=experiment_id,
    ):
        return  # ungated: nothing validates this scope

    await _block_worker_jobs_for_trials(session, llm_trial_ids)
    await _resolve_baseline_gate_for_scope(
        session,
        task_id=task_id,
        task_version_id=task_version_id,
        experiment_id=experiment_id,
    )
    # A FAULTY gate cancels the just-added LLM trials, which can make the task
    # "all trials done". Advance it (to QA or COMPLETED) in the same request so
    # an append/retry that gets gate-cancelled doesn't leave the task stuck in
    # RUNNING (with its verdict reset) until the next reconcile cycle. No-op
    # when trials are still BLOCKED/QUEUED.
    await maybe_start_qa_stage(session, llm_trial_ids[0])


async def maybe_advance_legacy_analyzing_task(
    session: AsyncSession, trial_id: str
) -> bool:
    """Advance a task stuck in the legacy ANALYZING stage to QA.

    New tasks never enter ANALYZING (they go RUNNING -> VERDICT_PENDING via
    :func:`maybe_start_qa_stage`). This only fires from the cleanup sweep for
    tasks left in ANALYZING by the pre-QA-refactor code: once every live
    trial's per-trial classification is terminal, enqueue the single QA job.

    Uses SELECT FOR UPDATE to prevent race conditions.
    """
    trial = await session.get(TrialModel, trial_id)
    if not trial:
        return False

    task_id = trial.task_id

    result = await session.execute(
        select(TaskModel).where(TaskModel.id == task_id).with_for_update()
    )
    task = result.scalar_one_or_none()

    if not task:
        return False

    if task.status != TaskStatus.ANALYZING:
        return False

    current_version_id = getattr(task, "current_version_id", None)
    if current_version_id is None:
        current_version_id = await session.scalar(
            select(TaskVersionModel.id)
            .where(TaskVersionModel.task_id == task_id)
            .order_by(TaskVersionModel.version.desc())
            .limit(1)
        )
        if current_version_id is not None:
            task.current_version_id = current_version_id

    if await session.scalar(
        select(func.count(TrialModel.id)).where(
            and_(
                TrialModel.task_id == task_id,
                TrialModel.task_version_id == current_version_id,
                TrialModel.superseded_by_trial_id.is_(None),
                # SKIPPED trials are never analyzed (analysis_status stays NULL),
                # so they must not count as pending or the task would never
                # advance out of ANALYZING.
                TrialModel.status != TrialStatus.SKIPPED,
                or_(
                    TrialModel.analysis_status.is_(None),
                    TrialModel.analysis_status.in_(
                        [
                            AnalysisStatus.PENDING,
                            AnalysisStatus.QUEUED,
                            AnalysisStatus.RUNNING,
                        ]
                    ),
                ),
            )
        )
    ):
        return False

    task.status = TaskStatus.VERDICT_PENDING
    task.verdict = None
    task.verdict_status = VerdictStatus.QUEUED
    task.verdict_error = None
    task.verdict_started_at = None
    task.verdict_finished_at = None
    await enqueue_qa_worker_job(
        session,
        task_id=task_id,
        org_id=task.org_id,
        task_version_id=current_version_id,
    )
    await session.flush()

    return True


# =============================================================================
# Query Helpers
# =============================================================================


# Valid per-queue status buckets. Shared by the per-org and grouped-by-org
# aggregators so both emit byte-identical queue-stat shapes.
_VALID_QUEUE_STATUSES = {
    "pending",
    "queued",
    "running",
    "success",
    "failed",
    "retrying",
    # Gate-skipped trials are terminal; count them (like success/failed) so they
    # don't vanish from a queue's per-status trial pipeline totals.
    "skipped",
}


def _empty_queue_counts() -> dict[str, int]:
    """Fresh zeroed status-count bucket for one queue key."""
    return {
        "pending": 0,
        "queued": 0,
        "running": 0,
        "success": 0,
        "failed": 0,
        "retrying": 0,
        "skipped": 0,
    }


def _accumulate_queue_stat(
    stats: dict[str, dict[str, int]],
    queue_key: str,
    status_name: str,
    count: int,
) -> None:
    """Add ``count`` into ``stats[normalize(queue_key)][status]``.

    Status is lower-cased and unknown statuses are ignored, matching the
    original ``get_queue_stats`` behavior. Factored out so the per-org and
    grouped-by-org aggregators share one normalization path and can't drift.
    """
    resolved_key = settings.normalize_queue_key(queue_key)
    status_key = status_name.lower()
    if status_key not in _VALID_QUEUE_STATUSES:
        return
    bucket = stats.setdefault(resolved_key, _empty_queue_counts())
    bucket[status_key] += int(count)


def _assemble_queue_and_pipeline(
    stats: dict[str, dict[str, int]],
) -> tuple[dict[str, dict], dict[str, dict[str, int]]]:
    """Shape raw per-queue-key counts into the dashboard ``(queue_stats,
    pipeline)`` payload.

    Zero-fills known queue keys, attaches ``recommended_concurrency``, and
    buckets counts into trial / analysis / verdict pipelines. Shared by the
    on-demand (``get_queue_and_pipeline_stats_with_concurrency``) and the
    precompute (``get_queue_and_pipeline_stats_by_org``) paths so a cached,
    precomputed value is identical to one computed live.
    """
    queue_stats: dict[str, dict] = {}
    queue_keys = set(stats.keys()) | settings.get_known_queue_keys()
    for queue_key in sorted(queue_keys):
        provider_stats = stats.get(queue_key, _empty_queue_counts())
        if queue_key in (ANALYSIS_PIPELINE_QUEUE_KEY, VERDICT_PIPELINE_QUEUE_KEY):
            # The pipeline buckets are not their own concurrency gates: both
            # classification and the verdict run inside the task-level QA
            # worker job, whose bucket is the analysis model's queue key.
            concurrency = settings.get_model_concurrency(settings.get_qa_queue_key())
        else:
            concurrency = settings.get_model_concurrency(queue_key)
        queue_stats[queue_key] = {
            **provider_stats,
            "recommended_concurrency": concurrency,
        }

    trial_pipeline: dict[str, int] = {}
    analysis_pipeline: dict[str, int] = {}
    verdict_pipeline: dict[str, int] = {}
    analysis_queue_key = ANALYSIS_PIPELINE_QUEUE_KEY
    verdict_queue_key = VERDICT_PIPELINE_QUEUE_KEY

    for queue_key, provider_stats in stats.items():
        for status_name, count in provider_stats.items():
            if queue_key == analysis_queue_key:
                analysis_pipeline[status_name] = analysis_pipeline.get(
                    status_name, 0
                ) + int(count)
            elif queue_key == verdict_queue_key:
                verdict_pipeline[status_name] = verdict_pipeline.get(
                    status_name, 0
                ) + int(count)
            else:
                trial_pipeline[status_name] = trial_pipeline.get(status_name, 0) + int(
                    count
                )

    return queue_stats, {
        "trials": trial_pipeline,
        "analyses": analysis_pipeline,
        "verdicts": verdict_pipeline,
    }


async def get_queue_stats(session: AsyncSession, org_id: str | None = None) -> dict:
    """Get queue statistics by queue_key across trial/analysis/verdict jobs.

    Trial counts are bucketed by the trial's own ``queue_key``. Analysis and
    verdict pipeline counts go into the reserved ``analysis`` / ``verdict``
    buckets (``ANALYSIS_PIPELINE_QUEUE_KEY`` / ``VERDICT_PIPELINE_QUEUE_KEY``),
    NOT the analysis/verdict model's queue key — keying them off a model merges
    pipeline state into that model's queue bucket and misreports both (e.g.
    thousands of trials mid-classification shown as "running" model workers).
    """
    stats: dict[str, dict[str, int]] = {}
    analysis_queue_key = ANALYSIS_PIPELINE_QUEUE_KEY
    verdict_queue_key = VERDICT_PIPELINE_QUEUE_KEY

    if org_id:
        result = await session.execute(
            text(
                """
                SELECT COALESCE(queue_key, provider) AS queue_key, status::text AS status, COUNT(*) AS count
                FROM trials
                WHERE org_id = :org_id
                  AND deleted_at IS NULL
                GROUP BY COALESCE(queue_key, provider), status
                """
            ),
            {"org_id": org_id},
        )
    else:
        result = await session.execute(
            text(
                """
                SELECT COALESCE(queue_key, provider) AS queue_key, status::text AS status, COUNT(*) AS count
                FROM trials
                WHERE deleted_at IS NULL
                GROUP BY COALESCE(queue_key, provider), status
                """
            )
        )

    for queue_key, status, count in result.all():
        _accumulate_queue_stat(stats, str(queue_key), str(status), int(count))

    analysis_query = (
        select(TrialModel.analysis_status, func.count(TrialModel.id))
        .where(TrialModel.analysis_status.isnot(None))
        .group_by(TrialModel.analysis_status)
    )
    if org_id:
        analysis_query = analysis_query.where(TrialModel.org_id == org_id)
    analysis_result = await session.execute(analysis_query)
    for analysis_status, count in analysis_result.all():
        _accumulate_queue_stat(
            stats, analysis_queue_key, analysis_status.value, int(count)
        )

    verdict_query = (
        select(TaskModel.verdict_status, func.count(TaskModel.id))
        .where(TaskModel.verdict_status.isnot(None))
        .group_by(TaskModel.verdict_status)
    )
    if org_id:
        verdict_query = verdict_query.where(TaskModel.org_id == org_id)
    verdict_result = await session.execute(verdict_query)
    for verdict_status, count in verdict_result.all():
        _accumulate_queue_stat(
            stats, verdict_queue_key, verdict_status.value, int(count)
        )

    return stats


async def get_queue_stats_by_org(
    session: AsyncSession,
) -> dict[str, dict[str, dict[str, int]]]:
    """Per-org queue stats from a single grouped pass over the tables.

    Returns ``{org_id: stats}`` where each ``stats`` has the exact shape
    ``get_queue_stats`` produces for one org. Used by the dashboard precompute
    to refresh every org's queue/pipeline slice in one scan instead of one
    scan per org. Rows with a NULL ``org_id`` are skipped (no org reads them).
    """
    analysis_queue_key = ANALYSIS_PIPELINE_QUEUE_KEY
    verdict_queue_key = VERDICT_PIPELINE_QUEUE_KEY
    stats_by_org: dict[str, dict[str, dict[str, int]]] = {}

    def _org_bucket(org_id: str) -> dict[str, dict[str, int]]:
        return stats_by_org.setdefault(org_id, {})

    trial_result = await session.execute(
        text(
            """
            SELECT org_id, COALESCE(queue_key, provider) AS queue_key,
                   status::text AS status, COUNT(*) AS count
            FROM trials
            WHERE deleted_at IS NULL
              AND org_id IS NOT NULL
            GROUP BY org_id, COALESCE(queue_key, provider), status
            """
        )
    )
    for org_id, queue_key, status, count in trial_result.all():
        _accumulate_queue_stat(
            _org_bucket(str(org_id)), str(queue_key), str(status), int(count)
        )

    analysis_result = await session.execute(
        select(
            TrialModel.org_id,
            TrialModel.analysis_status,
            func.count(TrialModel.id),
        )
        .where(TrialModel.analysis_status.isnot(None), TrialModel.org_id.isnot(None))
        .group_by(TrialModel.org_id, TrialModel.analysis_status)
    )
    for org_id, analysis_status, count in analysis_result.all():
        _accumulate_queue_stat(
            _org_bucket(str(org_id)),
            analysis_queue_key,
            analysis_status.value,
            int(count),
        )

    verdict_result = await session.execute(
        select(
            TaskModel.org_id,
            TaskModel.verdict_status,
            func.count(TaskModel.id),
        )
        .where(TaskModel.verdict_status.isnot(None), TaskModel.org_id.isnot(None))
        .group_by(TaskModel.org_id, TaskModel.verdict_status)
    )
    for org_id, verdict_status, count in verdict_result.all():
        _accumulate_queue_stat(
            _org_bucket(str(org_id)),
            verdict_queue_key,
            verdict_status.value,
            int(count),
        )

    return stats_by_org


async def get_queue_and_pipeline_stats_by_org(
    session: AsyncSession,
) -> dict[str, tuple[dict[str, dict], dict[str, dict[str, int]]]]:
    """Per-org ``(queue_stats, pipeline)`` payloads from one grouped scan.

    The precompute counterpart to ``get_queue_and_pipeline_stats_with_concurrency``:
    same per-org output shape, but computed for every org in a single pass so a
    scheduled job can warm the whole fleet's cache without scanning per org.
    """
    stats_by_org = await get_queue_stats_by_org(session)
    return {
        org_id: _assemble_queue_and_pipeline(stats)
        for org_id, stats in stats_by_org.items()
    }


async def get_queue_and_pipeline_stats_with_concurrency(
    session: AsyncSession, org_id: str | None = None
) -> tuple[dict[str, dict], dict[str, dict[str, int]]]:
    """Collect queue and pipeline stats without duplicating status scans."""
    stats = await get_queue_stats(session, org_id)
    return _assemble_queue_and_pipeline(stats)
