"""Build ReportEvalInputs (bundles + subanalyses) from trial rows.

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
from oddish.evals.report.bucketing import BUCKET_OF
from oddish.evals.report.schemas import ReportEvalInputs


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
    )


def _stub_bundle(trial: Any, task_path: str) -> TrajectoryBundle:
    return TrajectoryBundle(
        trial_id=trial.id, task_id=trial.task_id, task_path=task_path,
        agent=trial.agent, model=trial.model, reward=trial.reward,
        trajectory=[], logs={}, trajectory_summary=None, oracle_context=None,
        trajectory_link=trajectory_link(trial.task_id, trial.id),
    )


async def build_report_inputs(
    rows: list[tuple[Any, str]],
    *,
    read_trajectory: Callable[[Any], Awaitable[Any]] = read_trial_trajectory,
    read_logs: Callable[[Any], Awaitable[dict]] = read_trial_logs_structured,
) -> ReportEvalInputs:
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

    return ReportEvalInputs(bundles=bundles, subanalyses=subanalyses)
