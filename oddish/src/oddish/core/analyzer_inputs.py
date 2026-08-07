"""Build AnalyzerEvalInputs (bundles + subanalyses) from trial rows.

Trajectory content is read from S3 only for the bad/good failure trials that
the map step will actually analyze; success/harness trials get a stub bundle so
they still count toward num_trials without a needless S3 read. Readers are
injected so this is unit-testable without S3.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from oddish.core.trial_io import read_trial_logs_structured, read_trial_trajectory
from oddish.db.models import AnalysisStatus
from oddish.evals.primitives import SubAnalysis, TrajectoryBundle, trajectory_link
from oddish.evals.analyzer.bucketing import BUCKET_OF
from oddish.evals.analyzer.schemas import AnalyzerEvalInputs


_ORACLE_TRIAL_AGENT_CONTEXT = """
## Oracle Trial Context

This trial used the `oracle` agent. It is NOT a normal autonomous agent run.
The oracle is intentionally allowed to use the reference solution from
`solution/solve.sh` or `solution/fix.patch` to validate that the intended
solution passes the verifier. This context overrides the generic normal-agent
visibility guidance below.

For this oracle trial:
- Do NOT treat reading or applying `solution/` as cheating, oracle copying, test inspection, or suspicious agent behavior.
- A passing oracle usually means the reference solution validates correctly; classify it as GOOD_SUCCESS unless you find a real harness/test defect.
- A failing oracle is evidence that the reference solution, verifier, or task packaging is broken. Classify it as BAD_FAILURE when the failure is caused by the task artifacts, or HARNESS_ERROR only for infrastructure/runtime problems.
""".strip()

_NOP_TRIAL_AGENT_CONTEXT = """
## NoOp Trial Context

This trial used the `nop`/NoOp agent. It is a baseline validation run, not a
normal task-solving agent. The NoOp agent is expected to make no meaningful fix.

For this NoOp trial:
- A failing NoOp is normally GOOD_FAILURE because the task is not pre-solved.
- A passing NoOp is suspicious and should usually be BAD_SUCCESS or HARNESS_ERROR, depending on whether tests are too permissive or the harness malfunctioned.
- Do NOT judge NoOp behavior as though it attempted and failed to solve the task.
""".strip()


def trial_oracle_context(trial_agent: str | None) -> str:
    normalized_agent = (trial_agent or "").strip().lower()
    if normalized_agent == "oracle":
        return f"\n{_ORACLE_TRIAL_AGENT_CONTEXT}\n"
    if normalized_agent in {"nop", "noop", "no-op"}:
        return f"\n{_NOP_TRIAL_AGENT_CONTEXT}\n"
    return ""


# Old private name, still used below and by callers written against it.
_get_trial_agent_context = trial_oracle_context


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
