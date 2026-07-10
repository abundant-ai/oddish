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
    # TPU is only servable by GKE, so a submission that requests one via
    # override_tpu defaults there -- mirroring the CLI's TPU auto-routing for
    # everything expressible in the request body. Resolved by NAME (like the
    # CLI) so the default is right even when this deployment's registry never
    # registered the backend; ALLOWED_CLOUD_ENVIRONMENTS still rejects it
    # loudly on a GKE-less deployment. A TPU declared only in task.toml is not
    # visible here (the task tarball never passes through the API) -- those
    # submissions must pass environment=gke explicitly.
    if (
        submission is not None
        and submission.harbor.environment.override_tpu is not None
    ):
        return EnvironmentType.GKE
    requires_gpu = (
        submission is not None
        and (submission.harbor.environment.override_gpus or 0) > 0
    )
    return default_cloud_environment(requires_gpu=requires_gpu)
