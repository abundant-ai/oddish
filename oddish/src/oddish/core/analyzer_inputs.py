"""Build AnalyzerEvalInputs (bundles + subanalyses) from trial rows.

Trajectory content is read from S3 only for the bad/good failure trials that
the map step will actually analyze; success/harness trials get a stub bundle so
they still count toward num_trials without a needless S3 read. Readers are
injected so this is unit-testable without S3.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from oddish.analyze.classifier import _get_trial_agent_context
from oddish.core.trial_io import read_trial_logs_structured, read_trial_trajectory
from oddish.db.models import AnalysisStatus
from oddish.evals.primitives import SubAnalysis, TrajectoryBundle, trajectory_link
from oddish.evals.analyzer.bucketing import BUCKET_OF
from oddish.evals.analyzer.schemas import AnalyzerEvalInputs


def subanalysis_from_trial(trial: Any, task_path: str) -> SubAnalysis | None:
    if trial.analysis_status != AnalysisStatus.SUCCESS or not trial.analysis:
        return None
    a = trial.analysis
    return SubAnalysis(
        trial_id=trial.id,
        trajectory_link=trajectory_link(trial.task_id, trial.id),
        classification=a.get("classification", ""),
        subtype=a.get("subtype", ""),
        evidence=a.get("evidence", ""),
        root_cause=a.get("root_cause", ""),
        recommendation=a.get("recommendation", ""),
        trajectory_summary=getattr(trial, "trajectory_summary", None),
        model=getattr(trial, "model", None),
    )


def models_by_task_from_rows(rows: list[tuple[Any, str]]) -> dict[str, list[str]]:
    """task_id -> the distinct models that ran it, including trials that PASSED.

    Findings record only failures, so this is the sole source for "every model
    passed" -- without it, saturated and too-hard are indistinguishable.
    """
    by_task: dict[str, set[str]] = {}
    for trial, _task_path in rows:
        model = getattr(trial, "model", None)
        if model:
            by_task.setdefault(trial.task_id, set()).add(model)
    return {k: sorted(v) for k, v in by_task.items()}


def trial_model_rewards(rows: list[tuple[Any, str]]) -> list[tuple[str | None, float | None]]:
    """(raw model, reward) per gathered trial — the denominator input.

    Kept next to models_by_task_from_rows because both exist for the same
    reason: findings cover only failures, so anything needing a full-cohort
    denominator must read the trial rows instead.
    """
    return [
        (getattr(trial, "model", None), getattr(trial, "reward", None))
        for trial, _task_path in rows
    ]


def _stub_bundle(trial: Any, task_path: str) -> TrajectoryBundle:
    return TrajectoryBundle(
        trial_id=trial.id, task_id=trial.task_id, task_path=task_path,
        agent=trial.agent, model=trial.model, reward=trial.reward,
        trajectory=[], logs={}, trajectory_summary=None, oracle_context=None,
        trajectory_link=trajectory_link(trial.task_id, trial.id),
    )


async def build_analyzer_inputs(
    rows: list[tuple[Any, str]],
    *,
    read_trajectory: Callable[[Any], Awaitable[Any]] = read_trial_trajectory,
    read_logs: Callable[[Any], Awaitable[dict]] = read_trial_logs_structured,
) -> AnalyzerEvalInputs:
    subanalyses: list[SubAnalysis] = []
    bundles: list[TrajectoryBundle] = []

    for trial, task_path in rows:
        sa = subanalysis_from_trial(trial, task_path)
        if sa is not None:
            subanalyses.append(sa)

        bucket = BUCKET_OF.get(sa.classification, "other") if sa else "other"
        if bucket in ("bad", "good"):
            trajectory = await read_trajectory(trial)
            logs = await read_logs(trial)
            oracle = _get_trial_agent_context(trial.agent) if bucket == "bad" else ""
            bundles.append(
                TrajectoryBundle(
                    trial_id=trial.id, task_id=trial.task_id, task_path=task_path,
                    agent=trial.agent, model=trial.model, reward=trial.reward,
                    trajectory=(trajectory if isinstance(trajectory, list) else
                                (trajectory.get("steps", []) if isinstance(trajectory, dict) else [])),
                    logs=logs or {},
                    trajectory_summary=getattr(trial, "trajectory_summary", None),
                    oracle_context=(oracle.strip() or None),
                    trajectory_link=trajectory_link(trial.task_id, trial.id),
                )
            )
        else:
            bundles.append(_stub_bundle(trial, task_path))

    return AnalyzerEvalInputs(bundles=bundles, subanalyses=subanalyses)
