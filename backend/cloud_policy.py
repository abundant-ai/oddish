from __future__ import annotations

from harbor.models.environment_type import EnvironmentType

from oddish.runtime.routing import (
    allowed_cloud_environments,
    default_cloud_environment,
)
from oddish.schemas import TaskSweepSubmission

ALLOWED_CLOUD_ENVIRONMENTS = allowed_cloud_environments()


def get_default_cloud_environment(
    submission: TaskSweepSubmission | None = None,
) -> EnvironmentType:
    requires_gpu = (
        submission is not None
        and (submission.harbor.environment.override_gpus or 0) > 0
    )
    return default_cloud_environment(requires_gpu=requires_gpu)
