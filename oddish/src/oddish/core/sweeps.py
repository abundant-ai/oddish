from __future__ import annotations

from collections.abc import Collection

from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType

from oddish.config import settings
from oddish.schemas import TaskSubmission, TaskSweepSubmission, TrialSpec


def validate_sweep_submission(submission: TaskSweepSubmission) -> None:
    if not submission.configs:
        raise HTTPException(status_code=400, detail="Must specify 'configs'")


def _validate_allowed_environment(
    env: EnvironmentType,
    *,
    source: str,
    allowed_environments: Collection[EnvironmentType],
) -> None:
    if env not in allowed_environments:
        allowed = ", ".join(
            sorted(f"'{value.value}'" for value in allowed_environments)
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported sandbox environment in {source}: {env.value!r}. "
                f"Allowed values: {allowed}."
            ),
        )


def build_trial_specs_from_sweep(
    submission: TaskSweepSubmission,
    *,
    default_environment: EnvironmentType | None = None,
    allowed_environments: Collection[EnvironmentType] | None = None,
    existing_counts: dict[tuple[str, str | None], int] | None = None,
) -> list[TrialSpec]:
    trials: list[TrialSpec] = []
    effective_default_environment = submission.environment or default_environment
    if effective_default_environment and allowed_environments:
        _validate_allowed_environment(
            effective_default_environment,
            source="submission.environment",
            allowed_environments=allowed_environments,
        )

    for config in submission.configs:
        trial_environment = config.environment or effective_default_environment
        if trial_environment and allowed_environments:
            _validate_allowed_environment(
                trial_environment,
                source=f"configs[{config.agent}/{config.model or 'default'}].environment",
                allowed_environments=allowed_environments,
            )

        # Reconcile-to-N (declarative): in reconcile mode, emit only the
        # shortfall needed to bring the live count for this (agent, model)
        # up to the desired n_trials. In create mode (existing_counts is
        # None) emit the full n_trials -- today's additive behavior.
        n = config.n_trials
        if existing_counts is not None:
            # existing_counts is keyed by the trial's stored ``model`` column,
            # which is written through ``normalize_trial_model`` (see the
            # ``append_trials_to_task`` write path). Normalize the manifest's
            # raw ``config.model`` the same way so the lookup key matches the
            # stored (already-normalized) key. This match is the load-bearing
            # invariant: every trial write MUST normalize ``model``, and
            # ``normalize_trial_model`` MUST be idempotent. If a raw model ever
            # lands in the column, this ``.get`` misses, ``existing`` reads 0,
            # and reconcile silently re-appends a full N.
            norm_model = settings.normalize_trial_model(config.agent, config.model)
            existing = existing_counts.get((config.agent, norm_model), 0)
            n = max(0, config.n_trials - existing)

        for _ in range(n):
            trial_kwargs: dict = {
                "agent": config.agent,
                "model": config.model,
                "environment": trial_environment,
            }
            if config.agent_config:
                trial_kwargs["agent_config"] = config.agent_config
            trials.append(TrialSpec(**trial_kwargs))

    return trials


def build_task_submission_from_sweep(
    submission: TaskSweepSubmission,
    *,
    task_path: str,
    trials: list[TrialSpec],
) -> TaskSubmission:
    return TaskSubmission(
        task_path=task_path,
        name=submission.name,
        trials=trials,
        user=submission.user,
        priority=submission.priority,
        max_trial_attempts=submission.max_trial_attempts,
        experiment_id=submission.experiment_id,
        tags=submission.tags,
        run_analysis=submission.run_analysis,
        run_probe=submission.run_probe,
        github_username=submission.github_username,
        harbor=submission.harbor,
        content_hash=submission.content_hash,
        extra_instructions=submission.extra_instructions,
        probe_name=submission.probe_name,
        result_focus=submission.result_focus,
        probe_scope=submission.probe_scope,
        evaluation_metric=submission.evaluation_metric,
        link=submission.link,
    )
