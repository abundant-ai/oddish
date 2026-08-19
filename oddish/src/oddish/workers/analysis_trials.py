"""Analysis trials: the platform's own agents, run through the trial pipeline.

The pre-trial audit and QA are trials with a non-'agent' ``kind``. Each runs
claude-code on the analysis model in its own analysis sandbox, reads the
audited task's data through the oddish-query CLI, and writes one JSON
artifact. When the trial settles, the importer for its kind parses that
artifact into the same columns the old block-based path wrote, so nothing
downstream changes.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from importlib import resources

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.analyze import Classification, TrialClassification
from oddish.analyze.models import (
    _DIMENSION_HEADING_SPELLINGS,
    ActionItem,
    ActionItemSource,
    ActionTier,
    Dimension,
    ExploitationAssessment,
    ProblemType,
    TaskVerdictModel,
)
from oddish.analyze.trajectory_delegation import (
    delegation_facts,
    subagent_dispatches_in,
)
from oddish.analyze.trajectory_provenance import component_provenance
from oddish.analyze.trajectory_taxonomy import (
    SCHEMA_VERSION,
    render_summary_instructions,
    taxonomy_version,
)
from oddish.config import settings
from oddish.core.verdict_sync import (
    aggregate_exploited_into_pre_trial,
    build_pre_trial_payload,
    build_verdict_payload,
    complete_task_without_verdict,
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
from oddish.worker.analysis_result_check import check_analysis_result

logger = logging.getLogger(__name__)

ANALYSIS_TRIAL_KINDS = ("qa", "audit")
QA_RESULT_FILENAME = "qa_result.json"
AUDIT_RESULT_FILENAME = "audit_result.json"

# The one artifact each kind's agent writes to /logs. The analysis verifier
# stages it under /logs/verifier so harbor collects it.
ANALYSIS_ARTIFACTS = {
    "qa": QA_RESULT_FILENAME,
    "audit": AUDIT_RESULT_FILENAME,
}

ANALYSIS_TRIAL_MAX_ATTEMPTS = 3
ANALYSIS_TRIAL_TIMEOUT_MINUTES = 60


def is_analysis_kind(kind: str | None) -> bool:
    return kind in ANALYSIS_TRIAL_KINDS


def analysis_check_payload(kind: str, harbor_config: dict | None) -> dict:
    """The machine-checkable artifact contract for one analysis trial.

    Staged into the sandbox as ``expected.json`` for the generated verifier
    and passed verbatim to ``check_analysis_result`` by the importer -- one
    validator, two enforcement points. The value vocabularies are derived
    from the real enums and models here so the sandbox copy cannot drift
    from what the importers' parsers accept.
    """
    from typing import get_args

    item_vocabulary = {
        "sources": [s.value for s in ActionItemSource],
        "problem_types": [p.value for p in ProblemType],
        "dimensions": [d.value for d in Dimension],
        # The ActionItem model accepts the prompt's own heading spellings
        # for the dimension field; the validator must not be stricter.
        "dimension_spellings": sorted(_DIMENSION_HEADING_SPELLINGS),
        "tiers": [t.value for t in ActionTier],
    }
    if kind == "qa":
        payload = (harbor_config or {}).get("analysis_payload") or {}
        return {
            "kind": "qa",
            "trial_ids": [str(t) for t in payload.get("trial_ids") or []],
            "verdict_expected": bool(payload.get("with_verdict", True)),
            "classifications": [c.value for c in Classification],
            "verdicts": list(
                get_args(TaskVerdictModel.model_fields["verdict"].annotation)
            ),
            "confidences": list(
                get_args(TaskVerdictModel.model_fields["confidence"].annotation)
            ),
            **item_vocabulary,
        }
    return {"kind": "audit", **item_vocabulary}


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
        logger.info("created qa report experiment for %s (%s)", parent.id, parent.name)
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

    # Never burn LLM spend on a tombstone: a soft-deleted task (or a graded
    # version that no longer exists) means nobody can ever read the result.
    if task.deleted_at is not None:
        raise RuntimeError(
            f"refusing to create a {kind} trial for deleted task {task.id}"
        )
    version_to_pin = task_version_id or task.current_version_id
    version = None
    if version_to_pin is not None:
        version = await session.get(TaskVersionModel, version_to_pin)
        if version is None:
            raise RuntimeError(
                f"refusing to create a {kind} trial for task {task.id}: "
                f"version {version_to_pin} is missing"
            )

    if experiment_id is None:
        experiment_id = await resolve_analysis_experiment_id(session, task.id)
    next_index = await reserve_next_trial_index(session, task_id=task.id)
    trial_id = f"{task.id}-{next_index}"
    harbor_config: dict = {"mode": kind, "extra_instructions": brief}
    if kind == "audit" and version is not None and version.content_hash:
        # Pin the audited bytes. An in-place overwrite keeps the version id
        # while replacing its content, so the importer needs more than the
        # id to tell a stale audit from a current one.
        payload = {
            **(payload or {}),
            "task_version_content_hash": version.content_hash,
        }
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
    # Priority 1, not the default 0: analysis trials are enqueued after the
    # agent burst that produced them, and on pure FIFO one waited ~59 minutes
    # behind that backlog. The bump is what lets a draining worker pick the
    # QA/audit up ahead of it; agent trials keep priority 0.
    await enqueue_trial_worker_job(
        session,
        trial_id=trial_id,
        queue_key=trial.queue_key,
        org_id=task.org_id,
        max_attempts=ANALYSIS_TRIAL_MAX_ATTEMPTS,
        priority=1,
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


# A verdict needs enough evidence to be worth trusting: a handful of runs
# from more than one or two agents. Below this the task completes with its
# per-trial analysis and no verdict, rather than a confident call on noise.
MIN_VERDICT_TRIALS = 5
MIN_VERDICT_AGENTS = 3


async def has_verdict_evidence(session: AsyncSession, trial_ids: list[str]) -> bool:
    """Whether the eligible set can support a task verdict.

    ``trial_ids`` is the QA-eligible set, which already excludes baselines,
    probes, skipped, cancelled and superseded rows. Queries agents directly
    rather than touching a possibly-unloaded ``task.trials`` relationship.
    """
    if len(trial_ids) < MIN_VERDICT_TRIALS:
        return False
    agents = (
        await session.scalars(
            select(TrialModel.agent).where(TrialModel.id.in_(trial_ids))
        )
    ).all()
    return len({(a or "").strip().lower() for a in agents if a}) >= MIN_VERDICT_AGENTS


def build_qa_brief(
    *,
    task_name: str,
    trial_ids: list[str],
    pre_trial_items: list[dict] | None,
    with_verdict: bool = True,
) -> str:
    classify = _prompt("classify_prompt.txt")
    verdict = _prompt("verdict_prompt.txt")
    summary = render_summary_instructions(_prompt("prompts/trajectory_summary.txt"))
    pre_trial = (
        json.dumps(pre_trial_items, indent=1) if pre_trial_items else "(none recorded)"
    )
    ids = "\n".join(f"- {t}" for t in trial_ids)
    verdict_section = (
        f"== TASK VERDICT ==\nAfter classifying every trial, synthesize one task verdict:\n{verdict}\n"
        if with_verdict
        else '== TASK VERDICT ==\nDo NOT produce a verdict for this task: there are too few trials to judge it. Set "verdict": null in the output.\n'
    )
    # The output template must agree with the section above: showing the
    # verdict object shape while the prose says null would make the model
    # fail the (strict) verifier on every attempt.
    verdict_value = "<object matching this JSON schema>" if with_verdict else "null"
    verdict_schema = (
        "Verdict JSON schema:\n"
        f"{json.dumps(TaskVerdictModel.model_json_schema(), indent=1)}\n\n"
        if with_verdict
        else ""
    )
    return f"""You are the QA auditor for the task `{task_name}`. You are in a clean analysis sandbox, not the task's own environment. The task source, each trial's logs, and each trial's trajectory come from the oddish-query CLI. Do not solve the task.

Audit these trials:
{ids}

The oddish-query CLI fetches trial data from the oddish API (logs, trajectories, results, files). Run `node /probe-harness/oddish-query --help` first. Fetch each trial's trajectory and logs before judging it.

Known pre-trial audit findings for this task (do not repeat these as per-trial action items):
{pre_trial}

== PER-TRIAL CLASSIFICATION ==
{classify}

== PER-TRIAL TRAJECTORY SUMMARY ==
{summary}

{verdict_section}

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
      "trajectory_summary": <object with the exact shape given in the trajectory summary section>
    }}
  ],
  "verdict": {verdict_value}
}}

{verdict_schema}Every trial listed above must appear in "trials". The file must be valid JSON. Do not write anything else to /logs."""


def build_audit_brief(*, task_name: str) -> str:
    audit = _prompt("prompts/pre_trial_qa.txt")
    return f"""You are the pre-trial source auditor for the task `{task_name}`. Fetch the task source with the oddish-query CLI: run `node /probe-harness/oddish-query --help` first, then download the task's files. Do not solve the task.

{audit}

== OUTPUT ==
Write exactly one file: /logs/{AUDIT_RESULT_FILENAME}
It must hold the JSON object described in the OUTPUT section above: {{"items": [...]}} where every item carries the ten keys with the exact values that section defines. An empty "items" list means the source is clean. The file must be valid JSON. Do not write anything else to /logs."""


async def maybe_enqueue_audit_trial(
    session: AsyncSession, *, task: TaskModel, task_version_id: str | None
) -> bool:
    """Once per task version, CAS pre_trial_status None -> QUEUED and create
    the audit trial. Returns True when this call created it."""
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
    session: AsyncSession,
    *,
    task: TaskModel,
    eligible_trial_ids: list[str],
    with_verdict: bool = True,
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
            with_verdict=with_verdict,
        ),
        payload={"trial_ids": eligible_trial_ids, "with_verdict": with_verdict},
    )


async def read_artifact_bytes(trial: TrialModel, filename: str) -> bytes | None:
    """Find the artifact anywhere under the trial's storage prefix.

    Harbor nests each attempt's upload under its own job directory
    (``task-<name>__<rand>/``), so a fixed key never matches. The analysis
    verifier stages the artifact at ``<job dir>/verifier/<filename>``;
    prefer that, take any other ``/<filename>`` key as a fallback, and pick
    the newest when retries left several attempts behind."""
    prefix = resolve_trial_s3_prefix(trial.id, trial_s3_key=trial.trial_s3_key)
    storage = get_storage_client()
    try:
        objects = await storage.list_objects_all(prefix)
    except Exception:  # noqa: BLE001
        logger.exception("trial %s: listing %s failed", trial.id, prefix)
        return None
    staged = [
        o for o in objects if str(o.get("key", "")).endswith(f"/verifier/{filename}")
    ]
    loose = [o for o in objects if str(o.get("key", "")).endswith(f"/{filename}")]
    candidates = staged or loose
    if not candidates:
        return None
    newest = max(
        candidates,
        key=lambda o: (str(o.get("last_modified") or ""), str(o.get("key"))),
    )
    try:
        return await storage.download_bytes(str(newest["key"]))
    except Exception:  # noqa: BLE001
        logger.exception("trial %s: download %s failed", trial.id, newest["key"])
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


def enrich_trajectory_summary(
    summary: dict, *, trajectory: dict | None, model: str | None, graded_by: str
) -> dict:
    """Stamp a model-produced summary with the server-derived facts the old
    trajectory block computed: schema/taxonomy versions, and per-component
    ``tool_count`` / ``subagent_dispatches`` / ``duration_ms`` / file
    provenance. All arithmetic over the immutable trajectory, never model
    output (#1275); with no trajectory available only the version stamps are
    added."""
    from datetime import datetime

    out = {
        **summary,
        "schema_version": SCHEMA_VERSION,
        "taxonomy_version": taxonomy_version(),
        "model": model,
        "generated_at": utcnow().isoformat(),
        "_graded_by": graded_by,
    }
    components = out.get("components")
    if trajectory is None or not isinstance(components, list):
        return out

    steps = trajectory.get("steps") or []
    step_by_id = {
        step.get("step_id"): (index, step)
        for index, step in enumerate(steps)
        if isinstance(step, dict) and isinstance(step.get("step_id"), int)
    }

    def timestamp_ms(step: dict) -> float | None:
        value = step.get("timestamp")
        if not isinstance(value, str):
            return None
        try:
            return (
                datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
            )
        except ValueError:
            return None

    def duration_ms(index: int, step: dict) -> int:
        if index == 0:
            return 0
        current = timestamp_ms(step)
        previous = (
            timestamp_ms(steps[index - 1])
            if isinstance(steps[index - 1], dict)
            else None
        )
        if current is None or previous is None:
            return 0
        return max(0, round(current - previous))

    delegation = delegation_facts(trajectory)
    enriched = []
    for component in components:
        if not isinstance(component, dict):
            continue
        component_steps = [
            step_by_id[step_id]
            for step_id in component.get("step_ids") or []
            if step_id in step_by_id
        ]
        enriched.append(
            {
                **component,
                "tool_count": sum(
                    len(step.get("tool_calls") or [])
                    for _, step in component_steps
                    if isinstance(step.get("tool_calls"), list)
                ),
                # None, not 0, when the agent cannot delegate at all --
                # same distinction ``delegation.capable`` carries.
                "subagent_dispatches": (
                    subagent_dispatches_in([step for _, step in component_steps])
                    if delegation["capable"]
                    else None
                ),
                "duration_ms": sum(
                    duration_ms(index, step) for index, step in component_steps
                ),
                **component_provenance(trajectory, component_steps),
            }
        )
    out["components"] = enriched
    return out


async def _qa_import_still_current(
    session, task_id: str, graded_version_id: str | None
) -> bool:
    """A stale QA import must not complete the task out from under the
    fresh set. Two staleness modes: the task's current version moved past
    the version this QA trial was pinned to (re-upload mid-QA), or agent
    trials were appended after this QA trial started and are still running."""
    if graded_version_id is not None:
        current_version_id = await session.scalar(
            select(TaskModel.current_version_id).where(TaskModel.id == task_id)
        )
        if current_version_id is not None and current_version_id != graded_version_id:
            return False
    # Scope the pending check to the graded version, matching QA admission:
    # a historical version's still-live trial must not defer this import
    # forever (the healer would re-run it against the same live row on
    # every sweep until that unrelated trial ends).
    conditions = [
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
    ]
    if graded_version_id is not None:
        conditions.append(TrialModel.task_version_id == graded_version_id)
    pending = await session.scalar(select(TrialModel.id).where(*conditions).limit(1))
    return pending is None


async def _import_qa_result(trial: TrialModel) -> None:
    task_id = trial.task_id
    graded_version_id = trial.task_version_id
    artifact = None
    if trial.status == TrialStatus.SUCCESS:
        artifact = await read_analysis_artifact(trial, QA_RESULT_FILENAME)
    expected = analysis_check_payload("qa", trial.harbor_config)
    # A run below the evidence bar was told not to produce a verdict, so a
    # missing one is the expected outcome, not an import failure.
    verdict_expected = expected["verdict_expected"]
    # The same validator the in-sandbox verifier ran. Import is
    # all-or-nothing: a partial or malformed artifact must never publish a
    # subset of grades or a verdict built on one.
    violations = (
        check_analysis_result(artifact, expected) if artifact is not None else None
    )
    if artifact is None or violations:
        if trial.status != TrialStatus.SUCCESS:
            detail = (
                f"finished {trial.status.value}: "
                f"{trial.error_message or 'no error recorded'}"
            )
        elif artifact is None:
            detail = "produced no valid qa_result.json"
        else:
            detail = "artifact violates the QA contract: " + "; ".join(violations[:5])
        error = f"QA trial {trial.id} {detail}"
        logger.warning("qa import for task %s failed: %s", task_id, error)
        await sync_verdict_to_task(
            task_id,
            payload=None,
            should_store=lambda s: _qa_import_still_current(
                s, task_id, graded_version_id
            ),
            error=error,
        )
        return

    contract_drift: str | None = None
    classifications: list[TrialClassification] = []
    async with get_session() as session:
        for entry in artifact["trials"]:
            trial_id = entry["trial_id"]
            parsed = _classification_from_analysis(entry["analysis"])
            if parsed is None:
                # The shared validator accepted this artifact, so a parse
                # failure here is validator/importer drift. Refuse the whole
                # import (nothing committed) rather than storing a subset.
                contract_drift = (
                    f"analysis for {trial_id} failed to parse after passing validation"
                )
                break
            row = await session.get(TrialModel, trial_id)
            if row is None or row.task_id != task_id:
                # The graded row was deleted after the QA trial was staged;
                # that is not an artifact defect, so grade the rest.
                logger.warning(
                    "qa trial %s: graded trial %s no longer exists; skipping",
                    trial.id,
                    trial_id,
                )
                continue
            row.analysis = {**entry["analysis"], "_graded_by": trial.id}
            row.analysis_status = AnalysisStatus.SUCCESS
            row.analysis_finished_at = utcnow()
            # Enrichment reads the graded trial's own trajectory from
            # storage; a read failure must not lose the summary itself.
            trajectory = None
            try:
                from oddish.core.trial_io import read_trial_trajectory

                trajectory = await read_trial_trajectory(row)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "qa trial %s: trajectory read for %s failed; "
                    "storing summary without derived facts",
                    trial.id,
                    trial_id,
                )
            row.trajectory_summary = enrich_trajectory_summary(
                entry["trajectory_summary"],
                trajectory=trajectory,
                model=trial.model,
                graded_by=trial.id,
            )
            classifications.append(parsed)
        if contract_drift is None:
            await session.commit()
        else:
            # get_session commits on clean context exit; the rows written
            # before the drift was noticed must not land.
            await session.rollback()

    if contract_drift is not None:
        error = f"QA trial {trial.id}: {contract_drift}"
        logger.warning("qa import for task %s failed: %s", task_id, error)
        await sync_verdict_to_task(
            task_id,
            payload=None,
            should_store=lambda s: _qa_import_still_current(
                s, task_id, graded_version_id
            ),
            error=error,
        )
        return

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
            should_store=lambda s: _qa_import_still_current(
                s, task_id, graded_version_id
            ),
            error=f"QA trial {trial.id} artifact contained no valid classifications",
        )
        return
    if not verdict_expected:
        # Classifications are stored; the task completes with no verdict.
        # The caller fires the qa-imported hook after this returns.
        await complete_task_without_verdict(
            task_id,
            should_store=lambda s: _qa_import_still_current(
                s, task_id, graded_version_id
            ),
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
            should_store=lambda s: _qa_import_still_current(
                s, task_id, graded_version_id
            ),
            error=f"QA trial {trial.id} verdict failed validation: {exc}",
        )
        return
    payload = build_verdict_payload(verdict, classifications)
    payload["_graded_by"] = trial.id
    await sync_verdict_to_task(
        task_id,
        payload=payload,
        should_store=lambda s: _qa_import_still_current(s, task_id, graded_version_id),
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
    # An in-place overwrite keeps the version id but replaces its bytes (and
    # cancels live audits); this pin catches the race where the audit
    # settled first or was already importing. Old-bytes findings must never
    # land on the overwritten version.
    pinned_hash = ((trial.harbor_config or {}).get("analysis_payload") or {}).get(
        "task_version_content_hash"
    )
    if pinned_hash:
        async with get_session() as session:
            current_hash = await session.scalar(
                select(TaskVersionModel.content_hash).where(
                    TaskVersionModel.id == version_id
                )
            )
        if current_hash is not None and current_hash != pinned_hash:
            logger.warning(
                "audit trial %s: version %s content changed since the audit "
                "started (in-place overwrite); dropping its findings",
                trial.id,
                version_id,
            )
            return
    artifact = None
    if trial.status == TrialStatus.SUCCESS:
        artifact = await read_analysis_artifact(trial, AUDIT_RESULT_FILENAME)
    # The same validator the in-sandbox verifier ran: a malformed artifact
    # fails there and retries the agent, so reaching import with violations
    # means drift (or an old-format artifact) -- record the failure rather
    # than silently keeping a subset of findings.
    violations = (
        check_analysis_result(
            artifact, analysis_check_payload("audit", trial.harbor_config)
        )
        if artifact is not None
        else None
    )
    if artifact is None or violations:
        if trial.status != TrialStatus.SUCCESS:
            detail = f"finished {trial.status.value}"
        elif artifact is None:
            detail = "produced no valid audit_result.json"
        else:
            detail = "artifact violates the audit contract: " + "; ".join(
                violations[:5]
            )
        error = f"audit trial {trial.id} {detail}"
        logger.warning("audit import for version %s failed: %s", version_id, error)
        await sync_pre_trial_to_task_version(
            version_id,
            payload=None,
            error=RuntimeError(error),
            expected_content_hash=pinned_hash,
        )
        return
    items: list[ActionItem] = []
    for raw in artifact["items"]:
        try:
            items.append(ActionItem.model_validate(raw))
        except Exception as exc:  # noqa: BLE001
            # The validator accepted this item, so a parse failure is
            # validator/importer drift: refuse the import whole.
            error = (
                f"audit trial {trial.id}: finding failed to parse after "
                f"passing validation: {exc}"
            )
            logger.warning("audit import for version %s failed: %s", version_id, error)
            await sync_pre_trial_to_task_version(
                version_id,
                payload=None,
                error=RuntimeError(error),
                expected_content_hash=pinned_hash,
            )
            return
    # The early check above spared the artifact read, but only this locked
    # re-check (inside sync) closes the race with an in-place overwrite
    # committing between that check and this write.
    await sync_pre_trial_to_task_version(
        version_id,
        payload=build_pre_trial_payload(
            items, cost_usd=trial.cost_usd, block_id=trial.id
        ),
        error=None,
        expected_content_hash=pinned_hash,
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
        # QA admission defers while this audit is live (the QA brief embeds
        # the audit findings at creation). This settlement is what unblocks
        # it: without the re-entry, a task whose last agent trial settled
        # mid-audit would never start QA.
        from oddish.queue import maybe_start_task_qa_stage

        async with get_session() as session:
            await maybe_start_task_qa_stage(session, trial.task_id)
