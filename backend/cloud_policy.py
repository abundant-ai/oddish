from __future__ import annotations

from harbor.models.environment_type import EnvironmentType

ALLOWED_CLOUD_ENVIRONMENTS = frozenset({EnvironmentType.MODAL, EnvironmentType.DAYTONA})


def get_default_cloud_environment() -> EnvironmentType:
    return EnvironmentType.MODAL
