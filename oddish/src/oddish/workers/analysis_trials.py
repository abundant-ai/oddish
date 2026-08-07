"""Analysis trials: the platform's own agents, run through the trial pipeline.

QA, the pre-trial audit, and analyzer report generation are trials with a
non-'agent' ``kind``. Each runs claude-code on the analysis model inside the
task's own Harbor environment, reads peer data through the oddish-query CLI,
and writes one JSON artifact. When the trial settles, the importer for its
kind parses that artifact into the same columns the old block-based path
wrote, so nothing downstream changes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from importlib import resources

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.analyze import Classification, TrialClassification
from oddish.analyze.models import ActionItem, ExploitationAssessment, TaskVerdictModel
from oddish.analyze.trajectory_taxonomy import TrajectoryBlockTaxonomy
from oddish.config import settings
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
    TaskVersionModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    get_session,
    utcnow,
)
from oddish.db.storage import get_storage_client, resolve_trial_s3_prefix
from oddish.workers.queue.shared import console

logger = logging.getLogger(__name__)

ANALYSIS_TRIAL_KINDS = ("qa", "audit", "analyzer_map", "analyzer_reduce")
QA_RESULT_FILENAME = "qa_result.json"
AUDIT_RESULT_FILENAME = "audit_result.json"

ANALYSIS_TRIAL_MAX_ATTEMPTS = 3
ANALYSIS_TRIAL_TIMEOUT_MINUTES = 60


def is_analysis_kind(kind: str | None) -> bool:
    return kind in ANALYSIS_TRIAL_KINDS


# Hosted org-level audit opt-in (task_id -> enabled). oddish/ can't read the
# org settings table, so backend registers the check at container load.
_audit_enabled_fn: Callable[[str], Awaitable[bool]] | None = None


def register_audit_enabled_check(fn: Callable[[str], Awaitable[bool]]) -> None:
    global _audit_enabled_fn
    _audit_enabled_fn = fn


# Fired after a QA import writes the task verdict (hosted GitHub PR refresh).
_qa_imported_fn: Callable[[str], Awaitable[None]] | None = None


def register_qa_imported_hook(fn: Callable[[str], Awaitable[None]]) -> None:
    global _qa_imported_fn
    _qa_imported_fn = fn


async def _fire_qa_imported(task_id: str) -> None:
    if _qa_imported_fn is None:
        return
    try:
        await _qa_imported_fn(task_id)
    except Exception:  # noqa: BLE001
        logger.exception("qa imported hook failed for task %s", task_id)


def _prompt(name: str) -> str:
    return resources.files("oddish.analyze").joinpath(name).read_text()


async def resolve_analysis_experiment_id(session: AsyncSession, task_id: str) -> str:
    """Analysis trials live in a shadow experiment, not in the experiment
    they grade. Find the task's live (non-shadow) experiment, get-or-create
    its shadow, and join the task into the shadow so the shadow page can
    list it."""
    from sqlalchemy import text as sql_text

    from oddish.db.models import generate_id

    parent = (
        await session.execute(
            sql_text(
                """
                SELECT e.id, e.name, e.org_id, e.owner_user_id
                FROM task_experiments te
                JOIN experiments e ON e.id = te.experiment_id
                WHERE te.task_id = :task_id AND te.deleted_at IS NULL
                  AND e.deleted_at IS NULL AND e.shadow_of IS NULL
                ORDER BY te.created_at ASC LIMIT 1
                """
            ),
            {"task_id": task_id},
        )
    ).first()
    if parent is None:
        raise RuntimeError(
            f"task {task_id} has no live experiment membership for an analysis trial"
        )

    inserted = await session.execute(
        sql_text(
            """
            INSERT INTO experiments
                (id, name, org_id, owner_user_id, shadow_of,
                 is_public, is_collection, created_at, updated_at)
            VALUES
                (:id, :name, :org_id, :owner_user_id, :shadow_of,
                 false, false, NOW(), NOW())
            ON CONFLICT (shadow_of) WHERE deleted_at IS NULL DO NOTHING
            """
        ),
        {
            "id": generate_id(),
            "name": f"{parent.name[:240]} (qa report)",
            "org_id": parent.org_id,
            "owner_user_id": parent.owner_user_id,
            "shadow_of": parent.id,
        },
    )
    if getattr(inserted, "rowcount", 0):
        logger.info(
            "created qa report experiment for %s (%s)", parent.id, parent.name
        )
    shadow_id = await session.scalar(
        sql_text(
            "SELECT id FROM experiments "
            "WHERE shadow_of = :parent_id AND deleted_at IS NULL"
        ),
        {"parent_id": parent.id},
    )
    if shadow_id is None:
        raise RuntimeError(f"no shadow experiment for {parent.id}")

    await session.execute(
        sql_text(
            """
            INSERT INTO task_experiments (task_id, experiment_id, created_at)
            VALUES (:task_id, :experiment_id, NOW())
            ON CONFLICT (task_id, experiment_id) DO NOTHING
            """
        ),
        {"task_id": task_id, "experiment_id": str(shadow_id)},
    )
    return str(shadow_id)


async def create_analysis_trial(
    session: AsyncSession,
    *,
    task: TaskModel,
    kind: str,
    brief: str,
    task_version_id: str | None = None,
    payload: dict | None = None,
    experiment_id: str | None = None,
) -> TrialModel:
    from oddish.queue import enqueue_trial_worker_job, reserve_next_trial_index

    if experiment_id is None:
        experiment_id = await resolve_analysis_experiment_id(session, task.id)
    next_index = await reserve_next_trial_index(session, task_id=task.id)
    trial_id = f"{task.id}-{next_index}"
    harbor_config: dict = {"mode": kind, "extra_instructions": brief}
    if payload:
        harbor_config["analysis_payload"] = payload
    # Same normalize/provider/queue trio as agent-trial creation. The worker
    # re-normalizes strictly at pickup, so an unmapped model must fail here,
    # at create, not there.
    model = settings.normalize_trial_model("claude-code", settings.analysis_model)
    trial = TrialModel(
        id=trial_id,
        name=f"{task.name}-{kind}-{next_index}",
        task_id=task.id,
        task_version_id=task_version_id or task.current_version_id,
        experiment_id=experiment_id,
        org_id=task.org_id,
        billed_user_id=None,
        agent="claude-code",
        provider=settings.get_provider_for_trial("claude-code", model),
        queue_key=settings.get_queue_key_for_trial("claude-code", model),
        model=model,
        timeout_minutes=ANALYSIS_TRIAL_TIMEOUT_MINUTES,
        harbor_config=harbor_config,
        is_probe=False,
        kind=kind,
        max_attempts=ANALYSIS_TRIAL_MAX_ATTEMPTS,
        status=TrialStatus.QUEUED,
    )
    session.add(trial)
    await session.flush()
    await enqueue_trial_worker_job(
        session,
        trial_id=trial_id,
        queue_key=trial.queue_key,
        org_id=task.org_id,
        max_attempts=ANALYSIS_TRIAL_MAX_ATTEMPTS,
    )
    logger.info(
        "created %s trial %s for task %s (model=%s queue=%s experiment=%s)",
        kind,
        trial_id,
        task.id,
        trial.model,
        trial.queue_key,
        experiment_id,
    )
    return trial


def build_qa_brief(
    *,
    task_name: str,
    trial_ids: list[str],
    pre_trial_items: list[dict] | None,
) -> str:
    classify = _prompt("classify_prompt.txt")
    verdict = _prompt("verdict_prompt.txt")
    summary = _prompt("prompts/trajectory_summary.txt").replace(
        "{{taxonomy}}", ", ".join(m.value for m in TrajectoryBlockTaxonomy)
    )
    pre_trial = (
        json.dumps(pre_trial_items, indent=1) if pre_trial_items else "(none recorded)"
    )
    ids = "\n".join(f"- {t}" for t in trial_ids)
    return f"""You are the QA auditor for the task `{task_name}`. You are inside this task's own environment; the task source is in the working directory. Do not solve the task.

Audit these trials:
{ids}

The oddish-query CLI fetches trial data from the oddish API (logs, trajectories, results, files). Run `node /probe-harness/oddish-query --help` first. Fetch each trial's trajectory and logs before judging it.

Known pre-trial audit findings for this task (do not repeat these as per-trial action items):
{pre_trial}

== PER-TRIAL CLASSIFICATION ==
{classify}

== PER-TRIAL TRAJECTORY SUMMARY ==
{summary}

== TASK VERDICT ==
After classifying every trial, synthesize one task verdict:
{verdict}

== OUTPUT ==
Write exactly one file: /logs/{QA_RESULT_FILENAME}
{{
  "trials": [
    {{
      "trial_id": "<id>",
      "analysis": {{
        "trial_name": "<id>",
        "classification": "GOOD_SUCCESS|BAD_SUCCESS|GOOD_FAILURE|BAD_FAILURE|HARNESS_ERROR",
        "subtype": "...",
        "evidence": "...",
        "root_cause": "...",
        "recommendation": "...",
        "reward": <number or null>,
        "action_items": [],
        "exploitation": []
      }},
      "trajectory_summary": {{"schema_version": 5, "components": [{{"trajectory_component": "<taxonomy value>", "step_ids": [..], "summary": "..."}}]}}
    }}
  ],
  "verdict": <object matching this JSON schema>
}}

Verdict JSON schema:
{json.dumps(TaskVerdictModel.model_json_schema(), indent=1)}

Every trial listed above must appear in "trials". The file must be valid JSON. Do not write anything else to /logs."""


def build_audit_brief(*, task_name: str) -> str:
    audit = _prompt("prompts/pre_trial_qa.txt")
    return f"""You are the pre-trial source auditor for the task `{task_name}`. The task source is in the working directory. Do not solve the task.

{audit}

== OUTPUT ==
Write exactly one file: /logs/{AUDIT_RESULT_FILENAME}
{{"items": [{{"id": "...", "file": "...", "severity": "...", "description": "...", "suggestion": "..."}}]}}
An empty "items" list means the source is clean. The file must be valid JSON."""


async def maybe_enqueue_audit_trial(
    session: AsyncSession, *, task: TaskModel, task_version_id: str | None
) -> bool:
    """Once per task version, CAS pre_trial_status None -> QUEUED and create
    the audit trial. Returns True when this call created it."""
    if _audit_enabled_fn is not None:
        if not await _audit_enabled_fn(task.id):
            return False
    elif not settings.pre_trial_enabled:
        return False
    version_id = task_version_id or task.current_version_id
    if version_id is None:
        return False
    version = await session.get(TaskVersionModel, version_id, with_for_update=True)
    if version is None or version.pre_trial_status is not None:
        return False
    version.pre_trial_status = VerdictStatus.QUEUED
    version.pre_trial_started_at = utcnow()
    await create_analysis_trial(
        session,
        task=task,
        kind="audit",
        brief=build_audit_brief(task_name=task.name),
        task_version_id=version_id,
    )
    return True



async def create_qa_trial(
    session: AsyncSession, *, task: TaskModel, eligible_trial_ids: list[str]
) -> TrialModel:
    version = (
        await session.get(TaskVersionModel, task.current_version_id)
        if task.current_version_id
        else None
    )
    items = (version.pre_trial or {}).get("items") if version is not None else None
    return await create_analysis_trial(
        session,
        task=task,
        kind="qa",
        brief=build_qa_brief(
            task_name=task.name,
            trial_ids=eligible_trial_ids,
            pre_trial_items=items,
        ),
        payload={"trial_ids": eligible_trial_ids},
    )


async def read_artifact_bytes(trial: TrialModel, filename: str) -> bytes | None:
    prefix = resolve_trial_s3_prefix(trial.id, trial_s3_key=trial.trial_s3_key)
    storage = get_storage_client()
    for key in (
        f"{prefix}logs/{filename}",
        f"{prefix}{filename}",
        f"{prefix}logs/agent/{filename}",
    ):
        try:
            return await storage.download_bytes(key)
        except Exception:  # noqa: BLE001
            continue
    return None


async def read_analysis_artifact(trial: TrialModel, filename: str) -> dict | None:
    data = await read_artifact_bytes(trial, filename)
    if data is None:
        logger.warning("trial %s: no %s artifact in storage", trial.id, filename)
        return None
    try:
        parsed = json.loads(data)
    except Exception:  # noqa: BLE001
        logger.warning("trial %s: %s is not valid JSON", trial.id, filename)
        return None
    if not isinstance(parsed, dict):
        logger.warning("trial %s: %s is not a JSON object", trial.id, filename)
        return None
    return parsed


def _classification_from_analysis(analysis: dict) -> TrialClassification | None:
    try:
        return TrialClassification(
            trial_name=analysis.get("trial_name", ""),
            classification=Classification(analysis["classification"]),
            subtype=analysis.get("subtype", "Unknown"),
            evidence=analysis.get("evidence", ""),
            root_cause=analysis.get("root_cause", ""),
            recommendation=analysis.get("recommendation", ""),
            reward=analysis.get("reward"),
            action_items=[ActionItem(**x) for x in analysis.get("action_items", [])],
            exploitation=[
                ExploitationAssessment(**x) for x in analysis.get("exploitation", [])
            ],
        )
    except Exception:  # noqa: BLE001
        return None


async def _qa_import_still_current(session, task_id: str) -> bool:
    """A stale QA import (trials appended after this QA trial started) must
    not complete the task out from under the fresh set."""
    pending = await session.scalar(
        select(TrialModel.id)
        .where(
            TrialModel.task_id == task_id,
            TrialModel.kind == "agent",
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.status.in_(
                [
                    TrialStatus.PENDING,
                    TrialStatus.QUEUED,
                    TrialStatus.RUNNING,
                    TrialStatus.RETRYING,
                ]
            ),
        )
        .limit(1)
    )
    return pending is None


async def _import_qa_result(trial: TrialModel) -> None:
    task_id = trial.task_id
    artifact = None
    if trial.status == TrialStatus.SUCCESS:
        artifact = await read_analysis_artifact(trial, QA_RESULT_FILENAME)
    if artifact is None or not isinstance(artifact.get("verdict"), dict):
        error = f"QA trial {trial.id} " + (
            "produced no valid qa_result.json"
            if trial.status == TrialStatus.SUCCESS
            else f"finished {trial.status.value}: {trial.error_message or 'no error recorded'}"
        )
        logger.warning("qa import for task %s failed: %s", task_id, error)
        await sync_verdict_to_task(
            task_id,
            payload=None,
            should_store=lambda s: _qa_import_still_current(s, task_id),
            error=error,
        )
        return

    expected = set(
        (trial.harbor_config or {}).get("analysis_payload", {}).get("trial_ids", [])
    )
    classifications: list[TrialClassification] = []
    async with get_session() as session:
        for entry in artifact.get("trials", []):
            if not isinstance(entry, dict):
                continue
            trial_id = entry.get("trial_id")
            analysis = entry.get("analysis")
            if not trial_id or not isinstance(analysis, dict):
                continue
            if expected and trial_id not in expected:
                continue
            parsed = _classification_from_analysis(analysis)
            if parsed is None:
                logger.warning(
                    "qa trial %s: malformed analysis for %s rejected",
                    trial.id,
                    trial_id,
                )
                continue
            row = await session.get(TrialModel, trial_id)
            if row is None or row.task_id != task_id:
                logger.warning(
                    "qa trial %s: analysis for unknown trial %s dropped",
                    trial.id,
                    trial_id,
                )
                continue
            row.analysis = {**analysis, "_graded_by": trial.id}
            row.analysis_status = AnalysisStatus.SUCCESS
            row.analysis_finished_at = utcnow()
            summary = entry.get("trajectory_summary")
            if isinstance(summary, dict):
                row.trajectory_summary = summary
            classifications.append(parsed)
        await session.commit()

    try:
        await aggregate_exploited_into_pre_trial(task_id)
    except Exception:  # noqa: BLE001
        logger.exception("exploited-item aggregation failed for task %s", task_id)

    if not classifications:
        logger.warning(
            "qa trial %s: artifact for task %s had no valid classifications",
            trial.id,
            task_id,
        )
        await sync_verdict_to_task(
            task_id,
            payload=None,
            should_store=lambda s: _qa_import_still_current(s, task_id),
            error=f"QA trial {trial.id} artifact contained no valid classifications",
        )
        return
    try:
        verdict = TaskVerdictModel.model_validate(artifact["verdict"])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "qa trial %s: verdict for task %s failed validation: %s",
            trial.id,
            task_id,
            exc,
        )
        await sync_verdict_to_task(
            task_id,
            payload=None,
            should_store=lambda s: _qa_import_still_current(s, task_id),
            error=f"QA trial {trial.id} verdict failed validation: {exc}",
        )
        return
    payload = build_verdict_payload(verdict, classifications)
    payload["_graded_by"] = trial.id
    await sync_verdict_to_task(
        task_id,
        payload=payload,
        should_store=lambda s: _qa_import_still_current(s, task_id),
        error=None,
    )
    logger.info(
        "qa trial %s: stored %d classifications and verdict for task %s",
        trial.id,
        len(classifications),
        task_id,
    )


async def _import_audit_result(trial: TrialModel) -> None:
    version_id = trial.task_version_id
    if version_id is None:
        return
    artifact = None
    if trial.status == TrialStatus.SUCCESS:
        artifact = await read_analysis_artifact(trial, AUDIT_RESULT_FILENAME)
    if artifact is None or not isinstance(artifact.get("items"), list):
        error = f"audit trial {trial.id} " + (
            "produced no valid audit_result.json"
            if trial.status == TrialStatus.SUCCESS
            else f"finished {trial.status.value}"
        )
        logger.warning("audit import for version %s failed: %s", version_id, error)
        await sync_pre_trial_to_task_version(
            version_id,
            payload=None,
            error=RuntimeError(error),
        )
        return
    items: list[ActionItem] = []
    for raw in artifact["items"]:
        try:
            items.append(ActionItem.model_validate(raw))
        except Exception:  # noqa: BLE001
            logger.warning(
                "audit trial %s: malformed finding rejected: %r", trial.id, raw
            )
            continue
    await sync_pre_trial_to_task_version(
        version_id,
        payload=build_pre_trial_payload(
            items, cost_usd=trial.cost_usd, block_id=trial.id
        ),
        error=None,
    )
    logger.info(
        "audit trial %s: stored %d findings for version %s",
        trial.id,
        len(items),
        version_id,
    )


async def handle_analysis_trial_settled(trial_id: str) -> None:
    """Importer dispatch. Runs after a non-'agent' trial reaches a terminal
    status. Idempotent per kind: each importer's writers CAS or overwrite the
    same columns, so a double-fire re-imports the same artifact."""
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if (
            trial is None
            or trial.superseded_by_trial_id is not None
            or trial.harbor_stage == "cancelled"
            or trial.status
            not in (TrialStatus.SUCCESS, TrialStatus.FAILED, TrialStatus.SKIPPED)
        ):
            return
        kind = trial.kind
        status = trial.status.value
    logger.info("importing %s trial %s (status=%s)", kind, trial_id, status)
    if kind == "qa":
        await _import_qa_result(trial)
        await _fire_qa_imported(trial.task_id)
    elif kind == "audit":
        await _import_audit_result(trial)
    elif kind in ("analyzer_map", "analyzer_reduce"):
        from oddish.workers.analyzer_trials import advance_analyzer_pipeline

        await advance_analyzer_pipeline(trial)
