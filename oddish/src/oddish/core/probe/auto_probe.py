"""Auto-probe trigger: enqueue one probe trial per task version on sweep submit.

A probe is an ordinary sweep trial with ``extra_instructions`` set; the
``is_probe`` column is the indexed projection of ``harbor_config.mode ==
"probe"``. We enqueue through the low-level sweep path (not
``create_task_sweep_core``) so this never recurses into itself.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.config import next_probe_model
from oddish.core.sweeps import (
    build_task_submission_from_sweep,
    build_trial_specs_from_sweep,
)
from oddish.db import ExperimentModel, SkillModel, TaskModel, TrialModel
from oddish.queue import append_trials_to_task
from oddish.schemas import AgentModelPair, TaskSweepSubmission

logger = logging.getLogger(__name__)

# Auto-probes adopt the directive (operator prompt, result-focus, metric) from
# this seed skill, editable in one place. Agent is fixed to claude-code; the
# model still rotates per task version (``next_probe_model``). Missing -> the
# probe is logged and skipped (the real sweep is unaffected).
DEFAULT_PROBE_SKILL_ID = "cheat-detector"
DEFAULT_PROBE_AGENT = "claude-code"


async def _version_already_probed(session: AsyncSession, version_id: str) -> bool:
    hit = await session.scalar(
        select(TrialModel.id)
        .where(TrialModel.task_version_id == version_id, TrialModel.is_probe.is_(True))
        .limit(1)
    )
    return hit is not None


async def _probed_version_count(session: AsyncSession, org_id: str | None) -> int:
    stmt = select(func.count(func.distinct(TrialModel.task_version_id))).where(
        TrialModel.is_probe.is_(True)
    )
    if org_id is not None:
        stmt = stmt.where(TrialModel.org_id == org_id)
    return int(await session.scalar(stmt) or 0)


async def maybe_enqueue_auto_probe(
    session: AsyncSession,
    *,
    task: TaskModel,
    experiment: ExperimentModel | None,
    org_id: str | None,
) -> None:
    """Enqueue one probe trial for ``task``'s current version, if not already
    probed. Best-effort: any failure is logged and swallowed so a probe problem
    never blocks the caller's real sweep."""
    try:
        version_id = task.current_version_id
        if not version_id:
            return
        if await _version_already_probed(session, version_id):
            return

        model = next_probe_model(await _probed_version_count(session, org_id))

        skill = await session.scalar(
            select(SkillModel).where(SkillModel.id == DEFAULT_PROBE_SKILL_ID)
        )
        if skill is None or not skill.operator_prompt:
            logger.error(
                "Default probe skill %r not found (or has no operator_prompt); "
                "skipping auto-probe for task %s. Seed the skill to enable "
                "auto-probing.",
                DEFAULT_PROBE_SKILL_ID,
                task.id,
            )
            return

        submission = TaskSweepSubmission(
            task_id=task.id,
            append_to_task=True,
            name=task.name,
            configs=[AgentModelPair(agent=DEFAULT_PROBE_AGENT, model=model, n_trials=1)],
            extra_instructions=skill.operator_prompt,
            probe_name=skill.name,
            result_focus=skill.result_focus,
            evaluation_metric=skill.evaluation_metric,
            skill_ids=[skill.id],
            experiment_id=(experiment.id if experiment is not None else None),
            user=task.user,
        )
        trials = build_trial_specs_from_sweep(submission)
        expanded = build_task_submission_from_sweep(
            submission, task_path=task.task_path, trials=trials
        )
        new_trials = await append_trials_to_task(
            session,
            task=task,
            submission=expanded,
            experiment_id=submission.experiment_id,
        )

        from oddish.config import settings

        if settings.local_mode:
            import asyncio

            from oddish.worker.local_runner import run_trial_locally

            for trial in new_trials:
                asyncio.create_task(run_trial_locally(trial.id, dry_run=False))
    except Exception:  # noqa: BLE001 — best-effort; never fail the real sweep
        logger.exception(
            "auto-probe enqueue failed for task %s", getattr(task, "id", "?")
        )
