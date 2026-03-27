from __future__ import annotations

from collections.abc import Collection
import logging

from harbor.models.environment_type import EnvironmentType

from oddish.db import TrialModel, get_session

logger = logging.getLogger(__name__)

EnvironmentName = str | EnvironmentType


def _normalize_environment_value(environment: EnvironmentName | None) -> str | None:
    if environment is None:
        return None
    if isinstance(environment, EnvironmentType):
        return environment.value

    normalized = str(environment).strip().lower()
    return normalized or None


def normalize_environment(
    environment: str | None,
    *,
    allowed_environments: Collection[EnvironmentName],
    default_environment: EnvironmentName,
) -> str:
    default_value = _normalize_environment_value(default_environment)
    if default_value is None:
        raise ValueError("default_environment must not be empty")

    current = _normalize_environment_value(environment)
    if current is None:
        return default_value

    allowed_values = {
        value
        for value in (
            _normalize_environment_value(candidate)
            for candidate in allowed_environments
        )
        if value is not None
    }
    if current in allowed_values:
        return current

    return default_value


async def enforce_trial_environment(
    trial_id: str,
    *,
    allowed_environments: Collection[EnvironmentName],
    default_environment: EnvironmentName,
) -> None:
    """Rewrite a trial's environment to match the caller-provided policy."""
    default_value = normalize_environment(
        None,
        allowed_environments=allowed_environments,
        default_environment=default_environment,
    )
    async with get_session() as session:
        trial = await session.get(TrialModel, trial_id)
        if not trial:
            return

        current = _normalize_environment_value(trial.environment) or ""
        normalized = normalize_environment(
            trial.environment,
            allowed_environments=allowed_environments,
            default_environment=default_environment,
        )

        if current == normalized:
            return

        if current:
            logger.warning(
                "Overriding unsupported trial env %r -> %r (trial_id=%s)",
                trial.environment,
                default_value,
                trial_id,
            )

        trial.environment = normalized
        await session.commit()
