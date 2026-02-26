from __future__ import annotations

from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType

from oddish.schemas import TaskSubmission, TaskSweepSubmission, TrialSpec


def validate_sweep_submission(submission: TaskSweepSubmission) -> None:
    if not submission.configs:
        raise HTTPException(status_code=400, detail="Must specify 'configs'")


def _validate_allowed_environment(
    env: EnvironmentType,
    *,
    source: str,
    allowed_environments: set[EnvironmentType],
) -> None:
    if env not in allowed_environments:
        allowed = ", ".join(sorted(f"'{value.value}'" for value in allowed_environments))
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
    allowed_environments: set[EnvironmentType] | None = None,
) -> list[TrialSpec]:
    trials: list[TrialSpec] = []
    effective_default_environment = submission.environment or default_environment
    if effective_default_environment and allowed_environments:
        _validate_allowed_environment(
            effective_default_environment,
            source="submission.environment",
            allowed_environments=allowed_environments,
        )

    submission_timeout_explicit = "timeout_minutes" in submission.model_fields_set

    for config in submission.configs:
        trial_environment = config.environment or effective_default_environment
        if trial_environment and allowed_environments:
            _validate_allowed_environment(
                trial_environment,
                source=f"configs[{config.agent}/{config.model or 'default'}].environment",
                allowed_environments=allowed_environments,
            )

        config_timeout_explicit = "timeout_minutes" in config.model_fields_set
        timeout_explicit = config_timeout_explicit or submission_timeout_explicit
        timeout_minutes = config.timeout_minutes or submission.timeout_minutes

        for _ in range(config.n_trials):
            trial_kwargs: dict = {
                "agent": config.agent,
                "model": config.model,
                "environment": trial_environment,
            }
            # Keep Harbor task.toml timeout by default; only override when explicit.
            if timeout_explicit:
                trial_kwargs["timeout_minutes"] = timeout_minutes
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
        experiment_id=submission.experiment_id,
        tags=submission.tags,
        run_analysis=submission.run_analysis,
        disable_verification=submission.disable_verification,
        verifier_timeout_sec=submission.verifier_timeout_sec,
        env_cpus=submission.env_cpus,
        env_memory_mb=submission.env_memory_mb,
        env_storage_mb=submission.env_storage_mb,
        env_gpus=submission.env_gpus,
        env_gpu_types=submission.env_gpu_types,
        allow_internet=submission.allow_internet,
        agent_setup_timeout_sec=submission.agent_setup_timeout_sec,
        docker_image=submission.docker_image,
        mcp_servers=submission.mcp_servers,
        artifacts=submission.artifacts,
        sandbox_timeout_secs=submission.sandbox_timeout_secs,
        sandbox_idle_timeout_secs=submission.sandbox_idle_timeout_secs,
        auto_stop_interval_mins=submission.auto_stop_interval_mins,
        auto_delete_interval_mins=submission.auto_delete_interval_mins,
        snapshot_template_name=submission.snapshot_template_name,
    )
