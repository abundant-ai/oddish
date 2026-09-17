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
    # GPU need arrives two ways: an explicit override in the request body, or
    # the CLI's ``requires_gpu`` flag for a count declared only in task.toml.
    # A private-registry pull is visible from the credentials the submission
    # carries. Both are negotiated here, against THIS deployment's registry,
    # so a Thunder-less deployment picks Modal instead of rejecting the run.
    requires_gpu = submission is not None and (
        (submission.harbor.environment.override_gpus or 0) > 0
        or submission.requires_gpu
    )
    requires_private_registry = submission is not None and bool(
        submission.registry_auth
    )
    return default_cloud_environment(
        requires_gpu=requires_gpu,
        requires_private_registry=requires_private_registry,
    )
