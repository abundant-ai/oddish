"""Cross-task rollup of stored cohort comparisons. Read-only by construction."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.experiment_membership import trial_in_experiment
from oddish.db.models import TaskModel, TaskVersionModel, TrialModel

from api.services.blocks.analyzer.cohort.cohort_comparison_block import SCHEMA_VERSION
from api.services.cohort_comparison import _load_fresh_comparison, cohort_hash, resolve_cohorts
from api.services.cohort_metrics import CHART_CATEGORIES, THIN_N, cited_trials, delta

ROLLUP_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RollupVersion:
    task_version_id: str
    task_id: str
    task_name: str
    version: int


async def resolve_rollup_versions(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> list[RollupVersion]:
    """Distinct task versions an experiment's trials belong to.

    Membership is ``trial_in_experiment``, the repo's one predicate for this:
    an ordinary experiment owns trials through ``trials.experiment_id`` while a
    collection gathers them through ``experiment_trials``, and either filter
    alone silently returns nothing for the other kind. The predicate also drops
    combine-copy duplicates, which would otherwise inflate a cohort and shift
    every baseline computed from it.
    """
    rows = (
        await session.execute(
            select(
                TrialModel.task_version_id,
                TaskModel.id,
                TaskModel.name,
                TaskVersionModel.version,
            )
            .join(TaskVersionModel, TaskVersionModel.id == TrialModel.task_version_id)
            .join(TaskModel, TaskModel.id == TaskVersionModel.task_id)
            .where(
                trial_in_experiment(experiment_id),
                TaskModel.org_id == org_id,
                TrialModel.is_probe.is_(False),
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.task_version_id.is_not(None),
            )
            .distinct()
        )
    ).all()
    return [
        RollupVersion(
            task_version_id=r[0], task_id=r[1], task_name=r[2], version=r[3]
        )
        for r in rows
    ]


async def build_cohort_rollup(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> dict:
    """Models x categories over an experiment's already-compared task versions.

    Reads stored comparisons only. A version without a fresh one is reported
    in ``coverage.missing`` rather than generated: filling gaps on a GET would
    put an LLM call per task behind a page view.
    """
    versions = await resolve_rollup_versions(
        session, experiment_id=experiment_id, org_id=org_id
    )

    # model -> "success" | "failure" -> set of trial ids, over included versions
    cohort_by_model: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"success": set(), "failure": set()}
    )
    # (model, category) -> "success" | "failure" -> set of cited trial ids
    cited: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"success": set(), "failure": set()}
    )
    pooled: dict[str, dict[str, set[str]]] = defaultdict(
        lambda: {"success": set(), "failure": set()}
    )
    pooled_cohort = {"success": set(), "failure": set()}
    missing: list[dict] = []
    compared = 0

    for rv in versions:
        successful, failing = await resolve_cohorts(session, rv.task_version_id)
        comparison = await _load_fresh_comparison(
            session,
            task_id=rv.task_id,
            task_version_id=rv.task_version_id,
            current_hash=cohort_hash(
                [t["trial_id"] for t in successful], [t["trial_id"] for t in failing]
            ),
            schema_version=SCHEMA_VERSION,
        )
        if comparison is None:
            missing.append(
                {
                    "task_id": rv.task_id,
                    "task_name": rv.task_name,
                    "version": rv.version,
                    "reason": "not_compared",
                }
            )
            continue
        compared += 1

        model_of = await _models_for(
            session, [t["trial_id"] for t in successful] + [t["trial_id"] for t in failing]
        )
        for side, trials in (("success", successful), ("failure", failing)):
            for t in trials:
                model = model_of.get(t["trial_id"])
                if model:
                    cohort_by_model[model][side].add(t["trial_id"])
                pooled_cohort[side].add(t["trial_id"])

        for cat in comparison.get("categories") or []:
            name = cat.get("category")
            if name not in CHART_CATEGORIES:
                continue
            for side, key in (("success", "successful"), ("failure", "failing")):
                for trial_id in cited_trials(cat, key):
                    pooled[name][side].add(trial_id)
                    model = model_of.get(trial_id)
                    if model:
                        cited[(model, name)][side].add(trial_id)

    # Distinct trials cited anywhere for a model, across every category and both
    # sides. Summing the per-category ``n`` instead counts one trial once per
    # category it appears in, so a single run cited in six categories totals six
    # -- enough to clear a thin-evidence gate on its own.
    cited_runs: dict[str, set[str]] = defaultdict(set)
    for (model, _category), sides in cited.items():
        cited_runs[model] |= sides["success"] | sides["failure"]

    models = _model_rows(cohort_by_model, cited_runs)
    baselines = {m["model"]: m["baseline"] for m in models}
    pooled_baseline = _share(len(pooled_cohort["success"]), len(pooled_cohort["failure"]))

    categories = []
    for name in CHART_CATEGORIES:
        categories.append(
            {
                "category": name,
                "pooled": _cell(
                    len(pooled[name]["success"]),
                    len(pooled[name]["failure"]),
                    pooled_baseline,
                ),
                "per_model": [
                    {
                        "model": m["model"],
                        **_cell(
                            len(cited[(m["model"], name)]["success"]),
                            len(cited[(m["model"], name)]["failure"]),
                            baselines[m["model"]],
                        ),
                    }
                    for m in models
                ],
            }
        )

    return {
        "schema_version": ROLLUP_SCHEMA_VERSION,
        "coverage": {
            "task_versions_total": len(versions),
            "task_versions_compared": compared,
            "missing": missing,
        },
        "thin_threshold": THIN_N,
        "pooled_baseline": pooled_baseline,
        "models": models,
        "categories": categories,
    }


def _share(success: int, failure: int) -> float:
    total = success + failure
    return success / total if total else 0.0


def _cell(cited_success: int, cited_failure: int, baseline: float) -> dict:
    return {
        "cited_success": cited_success,
        "cited_failure": cited_failure,
        "n": cited_success + cited_failure,
        "ratio": (
            cited_success / (cited_success + cited_failure)
            if cited_success + cited_failure
            else None
        ),
        "delta": delta(cited_success, cited_failure, baseline),
    }


def _model_rows(
    cohort_by_model: dict[str, dict[str, set[str]]],
    cited_runs: dict[str, set[str]],
) -> list[dict]:
    rows = []
    for model, sides in cohort_by_model.items():
        rows.append(
            {
                "model": model,
                "cohort_success": len(sides["success"]),
                "cohort_failure": len(sides["failure"]),
                "baseline": _share(len(sides["success"]), len(sides["failure"])),
                "cited_runs": len(cited_runs.get(model) or ()),
            }
        )
    rows.sort(key=lambda r: r["cohort_success"] + r["cohort_failure"], reverse=True)
    return rows


async def _models_for(session: AsyncSession, trial_ids: list[str]) -> dict[str, str]:
    """trial id -> model, falling back to agent where model is NULL.

    ``trials.model`` is nullable and older rows predate it; the agent name is
    the coarser but always-present identity, and grouping a NULL under it beats
    dropping the trial's citations from the rollup entirely.
    """
    if not trial_ids:
        return {}
    rows = (
        await session.execute(
            select(TrialModel.id, TrialModel.model, TrialModel.agent).where(
                TrialModel.id.in_(trial_ids)
            )
        )
    ).all()
    return {r[0]: (r[1] or r[2]) for r in rows if (r[1] or r[2])}
