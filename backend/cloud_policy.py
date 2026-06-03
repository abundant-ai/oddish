from __future__ import annotations

from harbor.models.environment_type import EnvironmentType

from oddish.schemas import TaskSweepSubmission

ALLOWED_CLOUD_ENVIRONMENTS = frozenset({EnvironmentType.MODAL, EnvironmentType.DAYTONA})


def get_default_cloud_environment(
    submission: TaskSweepSubmission | None = None,
) -> EnvironmentType:
    if (
        submission is not None
        and (submission.harbor.environment.override_gpus or 0) > 0
    ):
        return EnvironmentType.MODAL
    return EnvironmentType.DAYTONA
