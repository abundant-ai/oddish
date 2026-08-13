"""Cross-task rollup of stored cohort comparisons. Read-only by construction."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.blocks.analyzer.analyzer_block import AnalyzerType
from oddish.core.experiment_membership import trial_in_experiment
from oddish.db.models import (
    AnalyzerBlockModel,
    JobStatus,
    TaskModel,
    TaskVersionModel,
    TrialModel,
)

from api.services.blocks.analyzer.cohort.cohort_comparison_block import SCHEMA_VERSION
from api.services.cohort_comparison import (
    FAILURE_CLASS,
    MIN_COHORT,
    SUCCESS_CLASS,
    _load_fresh_comparison,
    cohort_hash,
    resolve_cohorts,
)
from api.services.cohort_metrics import CHART_CATEGORIES, THIN_N, cited_trials, delta

ROLLUP_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class RollupVersion:
    task_version_id: str
    task_id: str
    task_name: str
    version: int


async def resolve_rollup_membership(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> tuple[list[RollupVersion], set[str]]:
    """An experiment's trials, and the distinct task versions they belong to.

    Membership is ``trial_in_experiment``, the repo's one predicate for this:
    an ordinary experiment owns trials through ``trials.experiment_id`` while a
    collection gathers them through ``experiment_trials``, and either filter
    alone silently returns nothing for the other kind. The predicate also drops
    combine-copy duplicates, which would otherwise inflate a cohort and shift
    every baseline computed from it.

    The trial ids come back alongside the versions because a task version is
    not owned by one experiment. ``resolve_cohorts`` takes every classified
    trial on a version, so the rollup needs this set to keep runs from other
    experiments out of its baselines and citations.
    """
    rows = (
        await session.execute(
            select(
                TrialModel.id,
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
    versions = {
        r[1]: RollupVersion(
            task_version_id=r[1], task_id=r[2], task_name=r[3], version=r[4]
        )
        for r in rows
    }
    return list(versions.values()), {r[0] for r in rows}


async def resolve_rollup_versions(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> list[RollupVersion]:
    """Distinct task versions an experiment's trials belong to."""
    versions, _ = await resolve_rollup_membership(
        session, experiment_id=experiment_id, org_id=org_id
    )
    return versions


async def build_cohort_rollup(
    session: AsyncSession, *, experiment_id: str, org_id: str
) -> dict:
    """Models x categories over an experiment's already-compared task versions.

    Reads stored comparisons only. A version without a fresh one is reported
    in ``coverage.missing`` rather than generated: filling gaps on a GET would
    put an LLM call per task behind a page view.
    """
    versions, members = await resolve_rollup_membership(
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
    missing_versions: list[RollupVersion] = []
    compared = 0

    # Which versions could possibly have a comparison, in one query, before the
    # loop. ``resolve_cohorts`` streams the ``trajectory_summary`` JSONB of every
    # classified trial (summaries here have run to hundreds of thousands of
    # characters), and this section is mounted on every experiment detail view
    # against a pool of two connections plus one overflow. Calling it per version
    # only to discard the result is the whole cost of the endpoint on an
    # experiment where most versions were never compared.
    stored = await _versions_with_stored_comparison(
        session, sorted({rv.task_id for rv in versions})
    )

    for rv in versions:
        # Definitively uncompared: no block names this version at all, so there
        # is nothing for the freshness check to read.
        if rv.task_version_id not in stored:
            missing_versions.append(rv)
            continue

        # Unscoped on purpose: the stored comparison was generated over the
        # whole task version, so its cohort hash is over the whole task
        # version too. Narrowing the cohort before hashing would never match
        # and every version would report as uncompared. Membership is applied
        # after the hash, to the trials that feed the rollup's own numbers.
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
            missing_versions.append(rv)
            continue
        compared += 1

        # A task version outlives the experiment that first ran it, and a
        # collection gathers a subset of the runs on one. Every trial past
        # this point is one this experiment owns; the rest belong to whoever
        # else ran the version, and counting them would make the radar a
        # picture of the task rather than of this experiment.
        cohort_ids = [
            t["trial_id"]
            for t in (*successful, *failing)
            if t["trial_id"] in members
        ]
        model_of = await _models_for(session, cohort_ids)
        for side, trials in (("success", successful), ("failure", failing)):
            for t in trials:
                if t["trial_id"] not in members:
                    continue
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
                    if trial_id not in members:
                        continue
                    pooled[name][side].add(trial_id)
                    model = model_of.get(trial_id)
                    if model:
                        cited[(model, name)][side].add(trial_id)

    missing = await _missing_rows(session, missing_versions)

    # Distinct trials cited anywhere for a model, across every category and both
    # sides. Summing the per-category ``n`` instead counts one trial once per
    # category it appears in, so a single run cited in six categories totals six
    # -- enough to clear a thin-evidence gate on its own.
    cited_runs: dict[str, set[str]] = defaultdict(set)
    for (model, _category), sides in cited.items():
        cited_runs[model] |= sides["success"] | sides["failure"]

    models = _model_rows(cohort_by_model, cited_runs)
    baselines = {m["model"]: m["baseline"] for m in models}
    comparable = {
        m["model"]: m["cohort_success"] > 0 and m["cohort_failure"] > 0 for m in models
    }
    pooled_baseline = _share(len(pooled_cohort["success"]), len(pooled_cohort["failure"]))
    pooled_comparable = bool(pooled_cohort["success"]) and bool(pooled_cohort["failure"])

    categories = []
    for name in CHART_CATEGORIES:
        categories.append(
            {
                "category": name,
                "pooled": _cell(
                    len(pooled[name]["success"]),
                    len(pooled[name]["failure"]),
                    pooled_baseline,
                    pooled_comparable,
                ),
                "per_model": [
                    {
                        "model": m["model"],
                        **_cell(
                            len(cited[(m["model"], name)]["success"]),
                            len(cited[(m["model"], name)]["failure"]),
                            baselines[m["model"]],
                            comparable[m["model"]],
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


async def _versions_with_stored_comparison(
    session: AsyncSession, task_ids: list[str]
) -> set[str]:
    """Task versions any SUCCESS comparison block names, in one query.

    A pre-filter, not a freshness check: a version in this set still goes
    through ``_load_fresh_comparison``, whose cohort-hash comparison is what
    keeps a stale comparison from being served. Its only job is to spare the
    versions that were definitively never compared an expensive
    ``resolve_cohorts`` call each.
    """
    if not task_ids:
        return set()
    rows = (
        await session.execute(
            select(AnalyzerBlockModel.block_metadata["task_version_id"].astext)
            .where(
                AnalyzerBlockModel.task_id.in_(task_ids),
                AnalyzerBlockModel.type == AnalyzerType.COHORT_COMPARISON.value,
                AnalyzerBlockModel.status == JobStatus.SUCCESS,
            )
            .distinct()
        )
    ).all()
    return {r[0] for r in rows if r[0]}


async def _missing_rows(
    session: AsyncSession, versions: list[RollupVersion]
) -> list[dict]:
    """Uncompared versions, each labelled with why it has no comparison.

    ``get_or_generate_comparison`` refuses any version whose larger cohort side
    is under ``MIN_COHORT``, so listing those as merely "not compared" invites
    the reader to go and generate something the gate will decline. The counts
    come from one grouped pass over ``trials.analysis`` -- no summary JSONB --
    and are an upper bound on the real cohort, since ``resolve_cohorts`` also
    drops classified trials that carry no trajectory summary. Erring that way
    keeps the label conservative: a version is only called gated when it cannot
    clear the bar even counting every classified trial.
    """
    if not versions:
        return []
    classification = TrialModel.analysis["classification"].astext
    rows = (
        await session.execute(
            select(
                TrialModel.task_version_id,
                func.count().filter(classification == SUCCESS_CLASS),
                func.count().filter(classification == FAILURE_CLASS),
            )
            .where(
                TrialModel.task_version_id.in_(
                    [v.task_version_id for v in versions]
                ),
                TrialModel.is_probe.is_(False),
                TrialModel.superseded_by_trial_id.is_(None),
                classification.in_([SUCCESS_CLASS, FAILURE_CLASS]),
            )
            .group_by(TrialModel.task_version_id)
        )
    ).all()
    largest_side: dict[str, int] = defaultdict(int)
    for version_id, successes, failures in rows:
        largest_side[version_id] = max(successes, failures)
    return [
        {
            "task_id": v.task_id,
            "task_name": v.task_name,
            "version": v.version,
            "reason": (
                "not_compared"
                if largest_side[v.task_version_id] >= MIN_COHORT
                else "below_cohort_gate"
            ),
        }
        for v in versions
    ]


def _share(success: int, failure: int) -> float:
    total = success + failure
    return success / total if total else 0.0


def _cell(
    cited_success: int, cited_failure: int, baseline: float, comparable: bool
) -> dict:
    """One category x model cell. ``ratio`` is evidence; ``delta`` is a claim.

    A one-sided cohort keeps its ratio -- the citations are real -- but loses
    its delta, because there is no other side for the baseline to mean
    anything against. See ``cohort_metrics.delta``.
    """
    return {
        "cited_success": cited_success,
        "cited_failure": cited_failure,
        "n": cited_success + cited_failure,
        "ratio": (
            cited_success / (cited_success + cited_failure)
            if cited_success + cited_failure
            else None
        ),
        "delta": delta(cited_success, cited_failure, baseline, comparable=comparable),
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
