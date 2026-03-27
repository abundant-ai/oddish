from __future__ import annotations

from harbor.models.environment_type import EnvironmentType

from oddish.environment_policy import (
    enforce_trial_environment as enforce_trial_environment_with_policy,
)

ALLOWED_CLOUD_ENVIRONMENTS = frozenset(
    {EnvironmentType.MODAL, EnvironmentType.DAYTONA}
)


def get_default_cloud_environment() -> EnvironmentType:
    return EnvironmentType.MODAL


async def enforce_trial_environment(trial_id: str) -> None:
    await enforce_trial_environment_with_policy(
        trial_id,
        allowed_environments=ALLOWED_CLOUD_ENVIRONMENTS,
        default_environment=get_default_cloud_environment(),
    )
