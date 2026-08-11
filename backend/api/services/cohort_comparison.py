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


def is_stale(block_metadata: dict | None, *, current_hash: str, schema_version: int) -> bool:
    """Freshness keys on the cohort hash AND the schema version.

    Keying on schema_version alone is the bug that left stored trajectory
    summaries serving a retired taxonomy forever.
    """
    if not block_metadata:
        return True
    return (
        block_metadata.get("cohort_hash") != current_hash
        or block_metadata.get("schema_version") != schema_version
    )


async def get_or_generate_comparison(
    session: AsyncSession,
    task_version_id: str,
    *,
    task_name: str,
    refresh: bool = False,
    triggered_by_user_id: str | None = None,
) -> dict | None:
    """Return the comparison, generating on miss. None when the gate fails."""
    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock, AnalyzerInput
    from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType

    from api.services.blocks.analyzer.cohort import cohort_prompts as cp
    from api.services.blocks.analyzer.cohort.cohort_comparison_block import (
        SCHEMA_VERSION,
        CohortComparisonBlock,
        CohortComparisonOutput,
        CohortInput,
    )
    from api.services.summarize_trajectory import resolve_summary_model

    successful, failing = await resolve_cohorts(session, task_version_id)
    if len(successful) < MIN_COHORT or len(failing) < MIN_COHORT:
        return None

    current = cohort_hash(
        [t["trial_id"] for t in successful], [t["trial_id"] for t in failing]
    )

    if not refresh:
        row = (
            await session.execute(
                select(AnalyzerBlockModel)
                .where(
                    AnalyzerBlockModel.task_id == task_version_id,
                    AnalyzerBlockModel.type == AnalyzerType.COHORT_COMPARISON.value,
                    AnalyzerBlockModel.status == JobStatus.SUCCESS,
                )
                .order_by(AnalyzerBlockModel.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row is not None and not is_stale(
            row.block_metadata, current_hash=current, schema_version=SCHEMA_VERSION
        ):
            return row.output

    model = resolve_summary_model()
    cb = CohortComparisonBlock(
        CohortInput(task_name=task_name, successful=successful, failing=failing),
        instructions_template=cp.load_cohort_prompt_template(),
    )
    block = AnalyzerBlock(
        analyzer_type=AnalyzerType.COHORT_COMPARISON,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(input={"task_version_id": task_version_id}),
        prompt=cb.build_prompt(),
        task_id=task_version_id,
        block_metadata={"schema_version": SCHEMA_VERSION, "cohort_hash": current},
        output_transform=cb.to_output,
        model=model,
        response_format=CohortComparisonOutput,
        output_schema=CohortComparisonOutput.model_json_schema(),
        triggered_by_user_id=triggered_by_user_id,
    )
    result = await block.run()
    filtered, drops = validate_evidence(result.output, successful, failing)
    filtered["dropped"] = drops
    # Surfaced in the UI so a reader can see when the comparison rests on thin
    # evidence, rather than the feature averaging over it silently.
    filtered["thin_coverage"] = [
        t["trial_id"] for t in (successful + failing) if t["coverage"] < 0.5
    ]
    return filtered
