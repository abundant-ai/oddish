"""Resolve, compare and cache the successful-vs-failing cohort comparison."""
from __future__ import annotations

import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.blocks.analyzer.analyzer_block import AnalyzerType
from oddish.db.models import AnalyzerBlockModel, JobStatus, TrialModel

# Below three trials per side the comparison is anecdote, not evidence.
MIN_COHORT = 3

SUCCESS_CLASS = "GOOD_SUCCESS"
FAILURE_CLASS = "GOOD_FAILURE"


def cohort_hash(success_ids: list[str], failure_ids: list[str]) -> str:
    """Identity of a cohort pair.

    Sorted so trial ordering does not churn the hash, and the two sides are
    separated so moving a trial between cohorts changes it.
    """
    payload = "|".join(sorted(success_ids)) + "//" + "|".join(sorted(failure_ids))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


async def _summaries_for(session: AsyncSession, trial_ids: list[str]) -> dict:
    """Latest SUCCESS trajectory summary per trial, in one DISTINCT ON pass."""
    if not trial_ids:
        return {}
    rows = (
        await session.execute(
            select(AnalyzerBlockModel.analyzer_id, AnalyzerBlockModel.output)
            .where(
                AnalyzerBlockModel.analyzer_id.in_(trial_ids),
                AnalyzerBlockModel.type == AnalyzerType.TRAJECTORY_SUMMARY.value,
                AnalyzerBlockModel.status == JobStatus.SUCCESS,
            )
            .order_by(
                AnalyzerBlockModel.analyzer_id,
                AnalyzerBlockModel.created_at.desc(),
            )
            .distinct(AnalyzerBlockModel.analyzer_id)
        )
    ).all()
    return {r[0]: (r[1] or {}) for r in rows}


async def resolve_cohorts(
    session: AsyncSession, task_version_id: str
) -> tuple[list[dict], list[dict]]:
    """Return (successful, failing) trials with their component streams.

    Classification lives in the ``analysis`` JSONB column, matching the filter
    in oddish/src/oddish/core/endpoints/tasks_query.py:1509.
    """
    out: dict[str, list[dict]] = {SUCCESS_CLASS: [], FAILURE_CLASS: []}
    for cls in (SUCCESS_CLASS, FAILURE_CLASS):
        ids = list(
            (
                await session.execute(
                    select(TrialModel.id).where(
                        TrialModel.task_version_id == task_version_id,
                        TrialModel.is_probe.is_(False),
                        TrialModel.analysis["classification"].astext == cls,
                    )
                )
            )
            .scalars()
            .all()
        )
        summaries = await _summaries_for(session, ids)
        for tid in ids:
            comps = [
                c
                for c in (summaries.get(tid, {}).get("components") or [])
                if isinstance(c, dict) and c.get("step_ids")
            ]
            # A trial with no summary contributes nothing citable.
            if not comps:
                continue
            # Coverage guards against the summariser's long-run defect: one
            # 2,940-step trial yields ~20 covered steps, so a comparison can
            # rest on far thinner evidence than its trial count suggests.
            all_ids = {i for c in comps for i in c["step_ids"]}
            span = max(all_ids)
            out[cls].append(
                {
                    "trial_id": tid,
                    "components": comps,
                    "covered_steps": len(all_ids),
                    "span": span,
                    "coverage": round(len(all_ids) / span, 3) if span else 0.0,
                }
            )
    return out[SUCCESS_CLASS], out[FAILURE_CLASS]


def _index(trials: list[dict]) -> dict[tuple, str]:
    """(trial_id, component, step_ids) -> the component's stored summary."""
    out: dict[tuple, str] = {}
    for t in trials:
        for c in t.get("components") or []:
            key = (
                t["trial_id"],
                c.get("trajectory_component"),
                tuple(sorted(c.get("step_ids") or [])),
            )
            out[key] = (c.get("summary") or "").strip()
    return out


def validate_evidence(
    output: dict, successful: list[dict], failing: list[dict]
) -> tuple[dict, dict]:
    """Drop citations that do not resolve against the stored summaries.

    This repo has had an analyzer fabricate trial ids at scale, so citations
    are verified rather than trusted. Evidence must name a component that
    exists, on the side of the comparison it was cited under, with the stored
    summary text unaltered.
    """
    index = {"successful": _index(successful), "failing": _index(failing)}
    drops = {"evidence": 0, "observations": 0, "categories": 0}

    kept_categories = []
    for cat in output.get("categories") or []:
        kept_sides: dict[str, list] = {}
        for side in ("successful", "failing"):
            kept_obs = []
            for obs in cat.get(side) or []:
                kept_ev = []
                for ev in obs.get("evidence") or []:
                    key = (
                        ev.get("trial_id"),
                        ev.get("trajectory_component"),
                        tuple(sorted(ev.get("step_ids") or [])),
                    )
                    stored = index[side].get(key)
                    if stored is not None and stored == (ev.get("quote") or "").strip():
                        kept_ev.append(ev)
                    else:
                        drops["evidence"] += 1
                if kept_ev:
                    kept_obs.append({**obs, "evidence": kept_ev})
                else:
                    drops["observations"] += 1
            kept_sides[side] = kept_obs
        if kept_sides["successful"] or kept_sides["failing"]:
            kept_categories.append({**cat, **kept_sides})
        else:
            drops["categories"] += 1

    return {**output, "categories": kept_categories}, drops
