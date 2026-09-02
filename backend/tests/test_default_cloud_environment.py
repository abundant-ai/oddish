"""The API's default-environment choice must mirror the CLI's TPU routing for
everything expressible in the request body: a submission that requests a TPU
(override_tpu) can only be served by GKE, so it must default there rather than
to the GPU/CPU cloud default.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cloud_policy
from cloud_policy import get_default_cloud_environment
from harbor.models.environment_type import EnvironmentType

from oddish.config import Settings, settings
from oddish.core.sweeps import build_trial_specs_from_sweep
from oddish.schemas import AgentModelPair, HarborConfig, TaskSweepSubmission


def _submission(**environment_fields) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="t1",
        configs=[AgentModelPair(agent="oracle")],
        harbor=HarborConfig.model_validate({"environment": environment_fields}),
    )


def _hash_in_bucket(bucket: int) -> str:
    return f"{bucket:064x}"


def test_override_tpu_defaults_to_gke() -> None:
    submission = _submission(override_tpu={"type": "v5e", "topology": "2x2"})
    assert (
        get_default_cloud_environment(submission, request_hash=_hash_in_bucket(0))
        == EnvironmentType.GKE
    )


def test_override_gpus_does_not_default_to_gke() -> None:
    submission = _submission(override_gpus=1)
    assert (
        get_default_cloud_environment(submission, request_hash=_hash_in_bucket(0))
        != EnvironmentType.GKE
    )


def test_plain_submission_does_not_default_to_gke() -> None:
    assert (
        get_default_cloud_environment(
            _submission(), request_hash=_hash_in_bucket(0)
        )
        != EnvironmentType.GKE
    )


def test_no_submission_does_not_default_to_gke() -> None:
    assert (
        get_default_cloud_environment(None, request_hash=_hash_in_bucket(0))
        != EnvironmentType.GKE
    )


def test_archil_percentage_uses_deterministic_request_bucket(monkeypatch) -> None:
    monkeypatch.setattr(settings, "archil_traffic_percent", 10)

    assert (
        get_default_cloud_environment(_submission(), request_hash=_hash_in_bucket(9))
        == EnvironmentType.ARCHIL
    )
    assert (
        get_default_cloud_environment(_submission(), request_hash=_hash_in_bucket(10))
        == EnvironmentType.DAYTONA
    )


def test_archil_percentage_zero_keeps_daytona(monkeypatch) -> None:
    monkeypatch.setattr(settings, "archil_traffic_percent", 0)

    assert (
        get_default_cloud_environment(_submission(), request_hash=_hash_in_bucket(0))
        == EnvironmentType.DAYTONA
    )


def test_archil_percentage_one_hundred_routes_every_hashed_cpu_sweep(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "archil_traffic_percent", 100)

    assert (
        get_default_cloud_environment(_submission(), request_hash=_hash_in_bucket(99))
        == EnvironmentType.ARCHIL
    )


def test_selected_default_reaches_every_unset_trial_in_the_sweep(monkeypatch) -> None:
    monkeypatch.setattr(settings, "archil_traffic_percent", 100)
    submission = TaskSweepSubmission(
        task_id="t1",
        configs=[
            AgentModelPair(agent="oracle", n_trials=2),
            AgentModelPair(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                n_trials=3,
            ),
        ],
    )
    selected = get_default_cloud_environment(
        submission, request_hash=_hash_in_bucket(0)
    )

    specs = build_trial_specs_from_sweep(
        submission,
        default_environment=selected,
        allowed_environments=cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS,
    )

    assert len(specs) == 5
    assert {spec.environment for spec in specs} == {EnvironmentType.ARCHIL}


def test_explicit_config_environment_wins_over_selected_archil_default(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "archil_traffic_percent", 100)
    submission = TaskSweepSubmission(
        task_id="t1",
        configs=[
            AgentModelPair(
                agent="oracle",
                n_trials=1,
                environment=EnvironmentType.MODAL,
            )
        ],
    )
    selected = get_default_cloud_environment(
        submission, request_hash=_hash_in_bucket(0)
    )

    specs = build_trial_specs_from_sweep(
        submission,
        default_environment=selected,
        allowed_environments=cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS,
    )

    assert specs[0].environment == EnvironmentType.MODAL


def test_archil_percentage_never_replaces_non_daytona_capability_route(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "archil_traffic_percent", 100)
    monkeypatch.setattr(
        cloud_policy,
        "default_cloud_environment",
        lambda **_kwargs: EnvironmentType.MODAL,
    )

    assert (
        get_default_cloud_environment(_submission(), request_hash=_hash_in_bucket(0))
        == EnvironmentType.MODAL
    )


@pytest.mark.parametrize("percentage", [-1, 101])
def test_archil_percentage_rejects_out_of_range_values(percentage: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, archil_traffic_percent=percentage)
