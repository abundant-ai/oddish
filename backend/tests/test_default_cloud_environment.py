"""The API's default-environment choice must mirror the CLI's TPU routing for
everything expressible in the request body: a submission that requests a TPU
(override_tpu) can only be served by GKE, so it must default there rather than
to the GPU/CPU cloud default.
"""

from __future__ import annotations

from cloud_policy import get_default_cloud_environment
from harbor.models.environment_type import EnvironmentType

from oddish.schemas import AgentModelPair, HarborConfig, TaskSweepSubmission


def _submission(**environment_fields) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="t1",
        configs=[AgentModelPair(agent="oracle")],
        harbor=HarborConfig.model_validate({"environment": environment_fields}),
    )


def test_override_tpu_defaults_to_gke() -> None:
    submission = _submission(override_tpu={"type": "v5e", "topology": "2x2"})
    assert get_default_cloud_environment(submission) == EnvironmentType.GKE


def test_override_gpus_does_not_default_to_gke() -> None:
    submission = _submission(override_gpus=1)
    assert get_default_cloud_environment(submission) != EnvironmentType.GKE


def test_plain_submission_does_not_default_to_gke() -> None:
    assert get_default_cloud_environment(_submission()) != EnvironmentType.GKE


def test_no_submission_does_not_default_to_gke() -> None:
    assert get_default_cloud_environment(None) != EnvironmentType.GKE
