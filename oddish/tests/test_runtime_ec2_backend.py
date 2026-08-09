from __future__ import annotations

import asyncio
import importlib
import os
import stat
import subprocess
import sys
import threading
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.environment_type import EnvironmentType

from oddish.config import Settings
from oddish.runtime.backends.ec2 import Ec2Backend
from oddish.runtime.ec2_policy import (
    AWS_ACCOUNT_ID_TAG_KEY,
    DEPLOYMENT_TAG_KEY,
    HARBOR_ENVIRONMENT_TAG_KEY,
    HARBOR_SESSION_TAG_KEY,
    HARBOR_TASK_TAG_KEY,
    MANAGED_TAG_KEY,
    NAME_TAG_KEY,
    TRIAL_ID_TAG_KEY,
    WORKER_JOB_ID_TAG_KEY,
)
from oddish.runtime.routing import default_cloud_environment
from oddish.schemas import TaskSweepSubmission

AWS_ACCOUNT_ID = "123456789012"
_ORIGINAL_RESOLVE_AWS_ACCOUNT_ID = Ec2Backend._resolve_aws_account_id
_ORIGINAL_VALIDATE_SSH_PRIVATE_KEY = Ec2Backend._validate_ssh_private_key


def _complete_ec2_settings(**overrides):
    values = {
        "ec2_enabled": True,
        "ec2_region": "us-east-1",
        "ec2_ami_id": "ami-123",
        "ec2_instance_type": "m7i-flex.large",
        "ec2_subnet_id": "subnet-123",
        "ec2_security_group_ids": ["sg-123"],
        "ec2_key_name": "oddish-harbor",
        "ec2_ssh_user": "ubuntu",
        "ec2_ssh_private_key": "PRIVATE KEY CONTENT",
        "ec2_aws_access_key_id": "EC2ACCESS",
        "ec2_aws_secret_access_key": "EC2SECRET",
        "ec2_aws_session_token": "EC2TOKEN",
        "ec2_root_volume_size_gb": 80,
        "ec2_use_public_ip": True,
        "ec2_bootstrap_docker": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _install_complete_ec2_settings(monkeypatch, **overrides):
    configured = _complete_ec2_settings(**overrides)
    import oddish.runtime.backends.ec2 as ec2_module

    monkeypatch.setattr(ec2_module, "settings", configured)
    monkeypatch.setattr(
        Ec2Backend,
        "_resolve_aws_account_id",
        lambda self: AWS_ACCOUNT_ID,
    )
    monkeypatch.setattr(
        Ec2Backend,
        "_validate_ssh_private_key",
        lambda self, path: None,
    )
    return configured


def _registered_backend_names(*, enabled: bool) -> list[str]:
    code = (
        "from oddish.runtime.registry import ordered_backends;"
        "print(','.join(backend.name for backend in ordered_backends()))"
    )
    ec2_names = {
        "ODDISH_EC2_ENABLED",
        "ODDISH_EC2_REGION",
        "ODDISH_EC2_AMI_ID",
        "ODDISH_EC2_INSTANCE_TYPE",
        "ODDISH_EC2_SUBNET_ID",
        "ODDISH_EC2_SECURITY_GROUP_IDS",
        "ODDISH_EC2_KEY_NAME",
        "ODDISH_EC2_SSH_USER",
        "ODDISH_EC2_SSH_PRIVATE_KEY",
        "ODDISH_EC2_AWS_ACCESS_KEY_ID",
        "ODDISH_EC2_AWS_SECRET_ACCESS_KEY",
        "ODDISH_EC2_AWS_SESSION_TOKEN",
        "ODDISH_EC2_ROOT_VOLUME_SIZE_GB",
        "ODDISH_EC2_USE_PUBLIC_IP",
        "ODDISH_EC2_BOOTSTRAP_DOCKER",
    }
    env = {key: value for key, value in os.environ.items() if key not in ec2_names}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    if enabled:
        env.update(
            {
                "ODDISH_EC2_ENABLED": "true",
                "ODDISH_EC2_REGION": "us-east-1",
                "ODDISH_EC2_AMI_ID": "ami-123",
                "ODDISH_EC2_SUBNET_ID": "subnet-123",
                "ODDISH_EC2_SECURITY_GROUP_IDS": '["sg-123"]',
                "ODDISH_EC2_KEY_NAME": "oddish-harbor",
                "ODDISH_EC2_SSH_PRIVATE_KEY": "PRIVATE KEY CONTENT",
                "ODDISH_EC2_AWS_ACCESS_KEY_ID": "EC2ACCESS",
                "ODDISH_EC2_AWS_SECRET_ACCESS_KEY": "EC2SECRET",
            }
        )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().split(",")


def _submission_with_ec2_environment(environment_payload: dict) -> TaskSweepSubmission:
    return TaskSweepSubmission.model_validate(
        {
            "task_id": "task-id",
            "name": "task",
            "configs": [{"agent": "nop", "n_trials": 1}],
            "environment": "ec2",
            "harbor": {"environment": environment_payload},
        }
    )


def test_ec2_settings_remain_optional_when_the_provider_is_disabled() -> None:
    configured = Settings(_env_file=None, ec2_enabled=False)

    assert configured.ec2_enabled is False


@pytest.mark.parametrize(
    "missing_field",
    [
        "ec2_region",
        "ec2_ami_id",
        "ec2_subnet_id",
        "ec2_security_group_ids",
        "ec2_key_name",
    ],
)
def test_enabling_ec2_without_every_required_launch_setting_fails_loudly(
    missing_field: str,
) -> None:
    overrides = {
        missing_field: [] if missing_field == "ec2_security_group_ids" else None
    }

    with pytest.raises(ValidationError, match=missing_field):
        _complete_ec2_settings(**overrides)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("ec2_region", "  \t"),
        ("ec2_ami_id", "\n"),
        ("ec2_subnet_id", " "),
        ("ec2_key_name", "  "),
        ("ec2_security_group_ids", ["sg-123", " "]),
    ],
)
def test_enabling_ec2_rejects_blank_required_strings_and_security_groups(
    field_name: str, value: object
) -> None:
    with pytest.raises(ValidationError, match=field_name):
        _complete_ec2_settings(**{field_name: value})


def test_enabling_ec2_with_private_networking_fails_for_the_public_ip_only_v1() -> None:
    with pytest.raises(ValidationError, match="ec2_use_public_ip"):
        _complete_ec2_settings(ec2_use_public_ip=False)


def test_enabling_ec2_with_a_nonpositive_root_volume_fails_loudly() -> None:
    with pytest.raises(ValidationError, match="ec2_root_volume_size_gb"):
        _complete_ec2_settings(ec2_root_volume_size_gb=0)


def test_enabling_ec2_rejects_a_blank_instance_profile() -> None:
    with pytest.raises(ValidationError, match="ec2_instance_profile"):
        _complete_ec2_settings(ec2_instance_profile="  ")


def test_enabling_ec2_does_not_require_the_worker_only_ssh_secret() -> None:
    configured = _complete_ec2_settings(ec2_ssh_private_key=None)

    assert configured.ec2_enabled is True
    assert configured.ec2_ssh_private_key is None


def test_ec2_registry_entry_is_absent_when_disabled() -> None:
    assert "ec2" not in _registered_backend_names(enabled=False)


def test_ec2_registry_entry_is_inserted_after_daytona_without_changing_defaults(
) -> None:
    names = _registered_backend_names(enabled=True)

    assert names.index("daytona") < names.index("ec2") < names.index("modal")
    assert default_cloud_environment(requires_gpu=False) == EnvironmentType.DAYTONA
    assert default_cloud_environment(requires_gpu=True) == EnvironmentType.MODAL


def test_ec2_backend_reports_cpu_memory_only_capabilities(monkeypatch) -> None:
    _install_complete_ec2_settings(monkeypatch)

    capabilities = Ec2Backend().capabilities()

    assert capabilities.gpu is None
    assert capabilities.tpu is None
    assert capabilities.network_egress == "allow"
    assert capabilities.persistent_volumes is False


def test_ec2_backend_materializes_the_private_key_once_with_owner_only_permissions(
    monkeypatch,
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    registrations = []
    import oddish.runtime.backends.ec2 as ec2_module

    monkeypatch.setattr(ec2_module.atexit, "register", registrations.append)
    backend = Ec2Backend()
    assert registrations == []

    first_path = backend.materialize_ssh_private_key()
    second_path = backend.materialize_ssh_private_key()

    try:
        assert first_path == second_path
        assert registrations == [
            backend._force_remove_materialized_worker_credentials
        ]
        assert first_path.read_text() == "PRIVATE KEY CONTENT\n"
        assert stat.S_IMODE(first_path.stat().st_mode) == 0o600
    finally:
        backend.remove_materialized_ssh_private_key()
    assert not first_path.exists()


def test_ec2_backend_fails_loudly_when_a_worker_materializes_without_the_key(
    monkeypatch,
) -> None:
    _install_complete_ec2_settings(monkeypatch, ec2_ssh_private_key=None)

    with pytest.raises(RuntimeError, match="ODDISH_EC2_SSH_PRIVATE_KEY"):
        Ec2Backend().materialize_ssh_private_key()


def test_ec2_backend_rejects_a_malformed_private_key_before_launch(
    monkeypatch,
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    monkeypatch.setattr(
        Ec2Backend,
        "_validate_ssh_private_key",
        _ORIGINAL_VALIDATE_SSH_PRIVATE_KEY,
    )
    backend = Ec2Backend()

    with pytest.raises(RuntimeError, match="multiline formatting"):
        backend.materialize_ssh_private_key()

    assert backend._ssh_key_path is None


def test_ec2_backend_materializes_namespaced_aws_credentials_as_a_0600_profile(
    monkeypatch,
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/existing/credentials")
    backend = Ec2Backend()

    profile_path = backend.materialize_aws_profile()
    try:
        assert stat.S_IMODE(profile_path.stat().st_mode) == 0o600
        assert profile_path.read_text() == (
            "[oddish-ec2]\n"
            "aws_access_key_id = EC2ACCESS\n"
            "aws_secret_access_key = EC2SECRET\n"
            "aws_session_token = EC2TOKEN\n"
        )
        assert os.environ["AWS_SHARED_CREDENTIALS_FILE"] == str(profile_path)
    finally:
        backend.remove_materialized_worker_credentials()

    assert not profile_path.exists()
    assert os.environ["AWS_SHARED_CREDENTIALS_FILE"] == "/existing/credentials"

    backend.remove_materialized_worker_credentials()
    assert os.environ["AWS_SHARED_CREDENTIALS_FILE"] == "/existing/credentials"


def test_ec2_credential_cleanup_preserves_existing_profile_after_validation_failure(
    monkeypatch,
) -> None:
    _install_complete_ec2_settings(monkeypatch, ec2_aws_access_key_id=None)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/existing/credentials")
    backend = Ec2Backend()

    with pytest.raises(RuntimeError, match="ODDISH_EC2_AWS_ACCESS_KEY_ID"):
        backend.materialize_aws_profile()

    backend.remove_materialized_worker_credentials()
    assert os.environ["AWS_SHARED_CREDENTIALS_FILE"] == "/existing/credentials"


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("ec2_aws_access_key_id", "ODDISH_EC2_AWS_ACCESS_KEY_ID"),
        ("ec2_aws_secret_access_key", "ODDISH_EC2_AWS_SECRET_ACCESS_KEY"),
    ],
)
def test_ec2_backend_fails_loudly_when_control_credentials_are_missing(
    monkeypatch, field: str, message: str
) -> None:
    _install_complete_ec2_settings(monkeypatch, **{field: None})

    with pytest.raises(RuntimeError, match=message):
        Ec2Backend().materialize_aws_profile()


def test_ec2_backend_supplies_fixed_platform_launch_settings_and_protected_tags(
    monkeypatch,
) -> None:
    configured = _install_complete_ec2_settings(monkeypatch)
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-pr-42")
    backend = Ec2Backend()

    kwargs = backend.harbor_env_kwargs({"tags": {"team": "evals"}})

    try:
        assert kwargs == {
            "region": configured.ec2_region,
            "ami_id": configured.ec2_ami_id,
            "instance_type": configured.ec2_instance_type,
            "subnet_id": configured.ec2_subnet_id,
            "security_group_ids": configured.ec2_security_group_ids,
            "key_name": configured.ec2_key_name,
            "ssh_key_path": str(backend.materialize_ssh_private_key()),
            "ssh_user": configured.ec2_ssh_user,
            "aws_profile": "oddish-ec2",
            "launch_mode": "ephemeral",
            "use_public_ip": True,
            "root_volume_size_gb": configured.ec2_root_volume_size_gb,
            "iam_instance_profile": None,
            "bootstrap_docker": configured.ec2_bootstrap_docker,
            "tags": {
                "team": "evals",
                MANAGED_TAG_KEY: "true",
                DEPLOYMENT_TAG_KEY: "oddish-pr-42",
                AWS_ACCOUNT_ID_TAG_KEY: AWS_ACCOUNT_ID,
            },
        }
    finally:
        backend.remove_materialized_worker_credentials()


def test_ec2_backend_supplies_optional_platform_instance_profile(monkeypatch) -> None:
    configured = _install_complete_ec2_settings(
        monkeypatch,
        ec2_instance_profile=(
            "arn:aws:iam::123456789012:instance-profile/task"
        ),
    )
    backend = Ec2Backend()

    kwargs = backend.harbor_env_kwargs({})

    try:
        assert kwargs["iam_instance_profile"] == configured.ec2_instance_profile
    finally:
        backend.remove_materialized_worker_credentials()


def test_ec2_credential_leases_preserve_shared_files_until_last_release(
    monkeypatch,
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    backend = Ec2Backend()

    backend.acquire_worker_credentials(include_ssh=True)
    profile_path = Path(os.environ["AWS_SHARED_CREDENTIALS_FILE"])
    key_path = backend.materialize_ssh_private_key()
    backend.acquire_worker_credentials(include_ssh=True)

    backend.release_worker_credentials()
    assert profile_path.exists()
    assert key_path.exists()

    backend.release_worker_credentials()
    assert not profile_path.exists()
    assert not key_path.exists()


def test_ec2_credential_lease_underflow_fails_loudly(monkeypatch) -> None:
    _install_complete_ec2_settings(monkeypatch)

    with pytest.raises(RuntimeError, match="without acquisition"):
        Ec2Backend().release_worker_credentials()


@pytest.mark.parametrize(
    "protected_name",
    [
        "region",
        "ami_id",
        "instance_type",
        "subnet_id",
        "security_group_ids",
        "key_name",
        "ssh_key_path",
        "ssh_user",
        "ssh_port",
        "ssh_connect_timeout_sec",
        "strict_host_key_checking",
        "ssh_known_hosts_path",
        "aws_profile",
        "launch_mode",
        "instance_id",
        "use_public_ip",
        "root_volume_size_gb",
        "root_volume_type",
        "root_device_name",
        "iam_instance_profile",
        "bootstrap_docker",
        "docker_ready_timeout_sec",
        "instance_ready_timeout_sec",
        "compose_up_timeout_sec",
        "metadata_options",
    ],
)
def test_ec2_backend_rejects_every_platform_owned_environment_kwarg(
    monkeypatch, protected_name: str
) -> None:
    _install_complete_ec2_settings(monkeypatch)

    with pytest.raises(ValueError, match=protected_name):
        Ec2Backend().harbor_env_kwargs({protected_name: "tenant-value"})


@pytest.mark.parametrize(
    "protected_tag",
    [
        NAME_TAG_KEY,
        HARBOR_ENVIRONMENT_TAG_KEY,
        HARBOR_SESSION_TAG_KEY,
        HARBOR_TASK_TAG_KEY,
        MANAGED_TAG_KEY,
        DEPLOYMENT_TAG_KEY,
        TRIAL_ID_TAG_KEY,
        WORKER_JOB_ID_TAG_KEY,
        AWS_ACCOUNT_ID_TAG_KEY,
    ],
)
def test_ec2_backend_rejects_user_attempts_to_replace_ownership_tags(
    monkeypatch, protected_tag: str
) -> None:
    _install_complete_ec2_settings(monkeypatch)

    with pytest.raises(ValueError, match=protected_tag):
        Ec2Backend().harbor_env_kwargs({"tags": {protected_tag: "tenant-value"}})


@pytest.mark.parametrize("tags", [None, False, 0, "", [], ()])
def test_ec2_backend_rejects_falsy_non_mapping_tags(
    monkeypatch, tags: object
) -> None:
    _install_complete_ec2_settings(monkeypatch)

    with pytest.raises(ValueError, match="tags.*mapping"):
        Ec2Backend().harbor_env_kwargs({"tags": tags})


def test_sweep_validates_each_resolved_config_environment() -> None:
    with pytest.raises(ValidationError, match="delete=false"):
        TaskSweepSubmission.model_validate(
            {
                "task_id": "task-id",
                "configs": [
                    {"agent": "nop", "environment": "docker", "n_trials": 1},
                    {"agent": "nop", "environment": "ec2", "n_trials": 1},
                ],
                "environment": "docker",
                "harbor": {"environment": {"delete": False}},
            }
        )


def test_sweep_does_not_validate_an_unused_ec2_default() -> None:
    submission = TaskSweepSubmission.model_validate(
        {
            "task_id": "task-id",
            "configs": [
                {"agent": "nop", "environment": "docker", "n_trials": 1},
                {"agent": "nop", "environment": "modal", "n_trials": 1},
            ],
            "environment": "ec2",
            "harbor": {"environment": {"delete": False}},
        }
    )

    assert submission.environment == EnvironmentType.EC2


@pytest.mark.parametrize(
    "environment_payload,expected_message",
    [
        ({"delete": False}, "delete=false"),
        ({"override_gpus": 1}, "GPU"),
        ({"override_tpu": {"type": "v5e", "topology": "2x2"}}, "TPU"),
        ({"kwargs": {"launch_mode": "attach"}}, "launch_mode"),
        ({"kwargs": {"iam_instance_profile": "role"}}, "iam_instance_profile"),
        (
            {"kwargs": {"metadata_options": {"HttpEndpoint": "enabled"}}},
            "metadata_options",
        ),
        ({"kwargs": {"region": "eu-west-1"}}, "region"),
    ],
)
def test_ec2_submission_rejects_unsupported_or_platform_owned_configuration(
    environment_payload: dict, expected_message: str
) -> None:
    with pytest.raises(ValidationError, match=expected_message):
        _submission_with_ec2_environment(environment_payload)


def test_ec2_submission_allows_cpu_and_memory_overrides_and_unprotected_tags() -> None:
    submission = _submission_with_ec2_environment(
        {
            "override_cpus": 4,
            "override_memory_mb": 8192,
            "kwargs": {"tags": {"team": "evals"}},
        }
    )

    assert submission.environment == EnvironmentType.EC2


def test_ec2_runner_revalidates_retention_and_accelerator_guards_before_dispatch(
    tmp_path,
) -> None:
    from oddish.workers.harbor.runner import run_harbor_trial_async

    invalid_environment_configs = [
        {"delete": False},
        {"override_gpus": 1},
        {"override_tpu": {"type": "v5e", "topology": "2x2"}},
        {"kwargs": {"launch_mode": "attach"}},
    ]

    for environment_config in invalid_environment_configs:
        with pytest.raises(ValueError):
            asyncio.run(
                run_harbor_trial_async(
                    task_path=tmp_path,
                    agent="nop",
                    jobs_dir=tmp_path / "jobs",
                    environment=EnvironmentType.EC2,
                    harbor_config={"environment": environment_config},
                )
            )


@pytest.mark.parametrize("variant_id", [None, "ephemeral"])
def test_ec2_runner_fails_before_either_engine_when_backend_is_unregistered(
    monkeypatch, tmp_path, variant_id: str | None
) -> None:
    from oddish.workers.harbor import runner as harbor_runner

    patch_requirements: list[bool] = []
    monkeypatch.setattr(
        harbor_runner,
        "apply_harbor_patches",
        lambda *, require_ec2: patch_requirements.append(require_ec2),
    )
    monkeypatch.setattr(harbor_runner, "get_backend", lambda _name: None)
    harbor_config = {"variant_id": variant_id} if variant_id is not None else {}

    with pytest.raises(RuntimeError, match="EC2.*not enabled|not registered"):
        asyncio.run(
            harbor_runner.run_harbor_trial_async(
                task_path=tmp_path,
                agent="nop",
                jobs_dir=tmp_path / "jobs",
                environment=EnvironmentType.EC2,
                harbor_config=harbor_config,
            )
        )
    assert patch_requirements == [True]


@pytest.mark.parametrize("exit_mode", ["success", "error", "cancel"])
def test_ec2_runner_releases_materialized_key_lease_for_every_ephemeral_exit(
    monkeypatch, tmp_path, exit_mode: str
) -> None:
    from oddish.workers.harbor import ephemeral, runner as harbor_runner

    class FakeBackend:
        def __init__(self) -> None:
            self.acquire_calls = 0
            self.release_calls = 0

        def acquire_worker_credentials(self, *, include_ssh: bool) -> None:
            assert include_ssh is True
            self.acquire_calls += 1

        def harbor_env_kwargs(self, base_kwargs: dict) -> dict:
            return base_kwargs

        def release_worker_credentials(self) -> None:
            self.release_calls += 1

    backend = FakeBackend()
    monkeypatch.setattr(
        harbor_runner, "apply_harbor_patches", lambda **_kwargs: None
    )
    monkeypatch.setattr(harbor_runner, "get_backend", lambda _name: backend)

    async def fake_child(**_kwargs):
        if exit_mode == "error":
            raise RuntimeError("child failed")
        if exit_mode == "cancel":
            raise asyncio.CancelledError
        return harbor_runner.HarborOutcome(
            reward=1.0,
            error=None,
            exit_code=0,
            duration_sec=0.1,
            job_result_path=None,
            job_dir=None,
        )

    monkeypatch.setattr(ephemeral, "run_ephemeral_harbor_trial", fake_child)
    coroutine = harbor_runner.run_harbor_trial_async(
        task_path=tmp_path,
        agent="nop",
        jobs_dir=tmp_path / "jobs",
        environment=EnvironmentType.EC2,
        harbor_config={"variant_id": "ephemeral"},
    )
    if exit_mode == "error":
        with pytest.raises(RuntimeError, match="child failed"):
            asyncio.run(coroutine)
    elif exit_mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(coroutine)
    else:
        asyncio.run(coroutine)

    assert backend.acquire_calls == 1
    assert backend.release_calls == 1


def _install_fake_boto3(monkeypatch, **clients: MagicMock) -> MagicMock:
    module = types.ModuleType("boto3")
    session = MagicMock()
    session.client.side_effect = lambda service, **_kwargs: clients[service]
    module.Session = MagicMock(return_value=session)
    monkeypatch.setitem(sys.modules, "boto3", module)
    return module.Session


def _ec2_handle(
    *,
    instance_id: str = "i-owned",
    account_id: str = AWS_ACCOUNT_ID,
    region: str = "us-east-1",
) -> str:
    return f"ec2://{account_id}/{region}/{instance_id}"


def _owned_instance_description(
    *,
    deployment: str = "oddish",
    account_id: str = AWS_ACCOUNT_ID,
    state: str = "running",
) -> dict:
    return {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-owned",
                        "State": {"Name": state},
                        "Tags": [
                            {"Key": MANAGED_TAG_KEY, "Value": "true"},
                            {"Key": DEPLOYMENT_TAG_KEY, "Value": deployment},
                            {"Key": AWS_ACCOUNT_ID_TAG_KEY, "Value": account_id},
                        ],
                    }
                ]
            }
        ]
    }


def test_ec2_teardown_terminates_an_instance_only_after_both_ownership_tags_match(
    monkeypatch, caplog
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    monkeypatch.setenv("MODAL_APP_NAME", "oddish")
    client = MagicMock()
    client.describe_instances.return_value = _owned_instance_description()
    client_factory = _install_fake_boto3(monkeypatch, ec2=client)

    with caplog.at_level("INFO"):
        result = asyncio.run(Ec2Backend().teardown(_ec2_handle()))

    assert result is True
    client_factory.assert_called_once_with(
        profile_name="oddish-ec2", region_name="us-east-1"
    )
    client.terminate_instances.assert_called_once_with(InstanceIds=["i-owned"])
    assert "metric=ec2_teardown outcome=terminated" in caplog.text


def test_ec2_cancellation_path_dispatches_terminate_instances_once(
    monkeypatch,
) -> None:
    from oddish.core.helpers import (
        cancel_job_by_worker,
        unregister_provider_teardown_delegate,
    )
    import oddish.runtime.registry as runtime_registry

    _install_complete_ec2_settings(monkeypatch)
    client = MagicMock()
    client.describe_instances.return_value = _owned_instance_description()
    _install_fake_boto3(monkeypatch, ec2=client)
    backend = Ec2Backend()
    monkeypatch.setattr(runtime_registry, "get_backend", lambda _name: backend)
    unregister_provider_teardown_delegate("ec2")

    try:
        result = asyncio.run(cancel_job_by_worker("ec2", _ec2_handle()))
    finally:
        backend.remove_materialized_worker_credentials()

    assert result is True
    client.terminate_instances.assert_called_once_with(InstanceIds=["i-owned"])


def test_ec2_stale_cleanup_path_dispatches_terminate_instances_once(
    monkeypatch,
) -> None:
    import oddish.runtime.registry as runtime_registry
    from oddish.workers.queue.cleanup import _terminate_orphaned_sandboxes

    _install_complete_ec2_settings(monkeypatch)
    client = MagicMock()
    client.describe_instances.return_value = _owned_instance_description()
    _install_fake_boto3(monkeypatch, ec2=client)
    backend = Ec2Backend()
    monkeypatch.setattr(runtime_registry, "get_backend", lambda _name: backend)

    try:
        terminated = asyncio.run(
            _terminate_orphaned_sandboxes({("ec2", _ec2_handle())})
        )
    finally:
        backend.remove_materialized_worker_credentials()

    assert terminated == 1
    client.terminate_instances.assert_called_once_with(InstanceIds=["i-owned"])


@pytest.mark.parametrize("state", ["shutting-down", "terminated"])
def test_ec2_teardown_treats_already_terminating_instance_as_success_without_second_call(
    monkeypatch, state: str
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    client = MagicMock()
    client.describe_instances.return_value = _owned_instance_description(state=state)
    _install_fake_boto3(monkeypatch, ec2=client)

    result = asyncio.run(Ec2Backend().teardown(_ec2_handle()))

    assert result is True
    client.terminate_instances.assert_not_called()


@pytest.mark.parametrize(
    "description",
    [
        {"Reservations": []},
        _owned_instance_description(deployment="another-deployment"),
        _owned_instance_description(account_id="999999999999"),
        {
            "Reservations": [
                {"Instances": [{"InstanceId": "i-owned", "Tags": []}]}
            ]
        },
    ],
)
def test_ec2_teardown_refuses_missing_or_wrongly_owned_instances(
    monkeypatch, caplog, description: dict
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    monkeypatch.setenv("MODAL_APP_NAME", "oddish")
    client = MagicMock()
    client.describe_instances.return_value = description
    _install_fake_boto3(monkeypatch, ec2=client)

    with caplog.at_level("WARNING"):
        result = asyncio.run(Ec2Backend().teardown(_ec2_handle()))

    assert result is False
    client.terminate_instances.assert_not_called()
    assert "metric=ec2_teardown outcome=refused" in caplog.text


def test_ec2_teardown_logs_and_returns_false_when_aws_raises(
    monkeypatch, caplog
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    client = MagicMock()
    client.describe_instances.side_effect = RuntimeError("aws unavailable")
    _install_fake_boto3(monkeypatch, ec2=client)

    with caplog.at_level("ERROR"):
        assert asyncio.run(Ec2Backend().teardown(_ec2_handle())) is False
    assert "metric=ec2_teardown outcome=error" in caplog.text


def test_ec2_teardown_returns_false_when_credentials_cannot_materialize(
    monkeypatch, caplog
) -> None:
    _install_complete_ec2_settings(monkeypatch, ec2_aws_access_key_id=None)

    with caplog.at_level("ERROR"):
        result = asyncio.run(Ec2Backend().teardown(_ec2_handle()))

    assert result is False
    assert "reason=credential_acquisition" in caplog.text


class _FakeDescribeInstancesPaginator:
    def __init__(self, pages: list[dict] | None = None, error: Exception | None = None):
        self.pages = pages or []
        self.error = error
        self.paginate = MagicMock(side_effect=self._paginate)

    def _paginate(self, **_kwargs):
        if self.error is not None:
            raise self.error
        return iter(self.pages)


def _inventory_instance(
    instance_id: str,
    *,
    state: str = "running",
    deployment: str = "oddish-pr-42",
    account_id: str = AWS_ACCOUNT_ID,
    launch_time: datetime | None = None,
    extra_tags: list[dict] | None = None,
) -> dict:
    return {
        "InstanceId": instance_id,
        "State": {"Name": state},
        "LaunchTime": launch_time or datetime(2026, 8, 7, 12, 30),
        "Tags": [
            {"Key": MANAGED_TAG_KEY, "Value": "true"},
            {"Key": DEPLOYMENT_TAG_KEY, "Value": deployment},
            {"Key": AWS_ACCOUNT_ID_TAG_KEY, "Value": account_id},
            {"Key": TRIAL_ID_TAG_KEY, "Value": "trial-1"},
            {"Key": WORKER_JOB_ID_TAG_KEY, "Value": "job-1"},
            *(extra_tags or []),
        ],
    }


def test_ec2_inventory_paginates_with_current_deployment_and_active_state_filters(
    monkeypatch,
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-pr-42")
    paginator = _FakeDescribeInstancesPaginator(
        [
            {
                "Reservations": [
                    {"Instances": [_inventory_instance("i-pending", state="pending")]}
                ]
            },
            {
                "Reservations": [
                    {
                        "Instances": [
                            _inventory_instance(
                                "i-stopped",
                                state="stopped",
                                launch_time=datetime(
                                    2026, 8, 7, 13, 30, tzinfo=timezone.utc
                                ),
                                extra_tags=[{"Key": "team", "Value": "evals"}],
                            )
                        ]
                    }
                ]
            },
        ]
    )
    ec2 = MagicMock()
    ec2.get_paginator.return_value = paginator
    session_factory = _install_fake_boto3(monkeypatch, ec2=ec2)
    backend = Ec2Backend()

    inventory = asyncio.run(backend.snapshot_managed_instances())
    snapshots = inventory.instances

    assert [snapshot.instance_id for snapshot in snapshots] == [
        "i-pending",
        "i-stopped",
    ]
    assert snapshots[0].state == "pending"
    assert snapshots[0].launch_time == datetime(
        2026, 8, 7, 12, 30, tzinfo=timezone.utc
    )
    assert snapshots[1].state == "stopped"
    assert snapshots[1].managed_tag == "true"
    assert snapshots[1].deployment_tag == "oddish-pr-42"
    assert snapshots[1].account_id_tag == AWS_ACCOUNT_ID
    assert snapshots[1].trial_id_tag == "trial-1"
    assert snapshots[1].worker_job_id_tag == "job-1"
    assert all(snapshot.region == "us-east-1" for snapshot in snapshots)
    assert inventory.expected_account_id == AWS_ACCOUNT_ID
    assert inventory.expected_deployment == "oddish-pr-42"
    session_factory.assert_called_once_with(
        profile_name="oddish-ec2", region_name="us-east-1"
    )
    client_call = session_factory.return_value.client.call_args
    client_config = client_call.kwargs["config"]
    assert client_config.connect_timeout == 5
    assert client_config.read_timeout == 10
    assert client_config.retries == {"max_attempts": 3, "mode": "standard"}
    ec2.get_paginator.assert_called_once_with("describe_instances")
    paginator.paginate.assert_called_once_with(
        Filters=[
            {"Name": f"tag:{MANAGED_TAG_KEY}", "Values": ["true"]},
            {
                "Name": f"tag:{DEPLOYMENT_TAG_KEY}",
                "Values": ["oddish-pr-42"],
            },
            {
                "Name": "instance-state-name",
                "Values": [
                    "pending",
                    "running",
                    "stopping",
                    "stopped",
                    "shutting-down",
                ],
            },
        ]
    )
    profile_path = backend._aws_profile_path
    assert profile_path is not None and profile_path.exists()
    backend.remove_materialized_worker_credentials()
    assert backend._aws_profile_path is None
    assert not profile_path.exists()


@pytest.mark.parametrize(
    ("raw_state", "normalized_state"),
    [
        ("pending", "pending"),
        ("RUNNING", "running"),
        (" stopping ", "stopping"),
        ("stopped", "stopped"),
        ("shutting-down", "shutting-down"),
    ],
)
def test_ec2_inventory_normalizes_every_active_state(
    monkeypatch, raw_state: str, normalized_state: str
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-pr-42")
    paginator = _FakeDescribeInstancesPaginator(
        [
            {
                "Reservations": [
                    {
                        "Instances": [
                            _inventory_instance("i-state", state=raw_state)
                        ]
                    }
                ]
            }
        ]
    )
    ec2 = MagicMock()
    ec2.get_paginator.return_value = paginator
    _install_fake_boto3(monkeypatch, ec2=ec2)
    backend = Ec2Backend()

    try:
        inventory = asyncio.run(backend.snapshot_managed_instances())
    finally:
        backend.remove_materialized_worker_credentials()

    assert len(inventory.instances) == 1
    assert inventory.instances[0].state == normalized_state


def test_ec2_inventory_defensively_refuses_wrong_tags_and_inactive_states(
    monkeypatch, caplog
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-pr-42")
    paginator = _FakeDescribeInstancesPaginator(
        [
            {
                "Reservations": [
                    {
                        "Instances": [
                            _inventory_instance(
                                "i-wrong-deployment", deployment="oddish-production"
                            ),
                            _inventory_instance(
                                "i-wrong-account", account_id="999999999999"
                            ),
                            _inventory_instance("i-terminated", state="terminated"),
                            _inventory_instance("i-running"),
                        ]
                    }
                ]
            }
        ]
    )
    ec2 = MagicMock()
    ec2.get_paginator.return_value = paginator
    _install_fake_boto3(monkeypatch, ec2=ec2)
    backend = Ec2Backend()

    try:
        with caplog.at_level("WARNING"):
            inventory = asyncio.run(backend.snapshot_managed_instances())
    finally:
        backend.remove_materialized_worker_credentials()

    assert [snapshot.instance_id for snapshot in inventory.instances] == ["i-running"]
    assert "metric=ec2_inventory_ownership_refusal" in caplog.text
    assert "metric=ec2_inventory_state_refusal" in caplog.text


def test_ec2_inventory_logs_metric_and_raises_when_aws_listing_fails(
    monkeypatch, caplog
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    paginator = _FakeDescribeInstancesPaginator(error=RuntimeError("aws unavailable"))
    ec2 = MagicMock()
    ec2.get_paginator.return_value = paginator
    _install_fake_boto3(monkeypatch, ec2=ec2)
    backend = Ec2Backend()

    try:
        with caplog.at_level("ERROR"), pytest.raises(
            RuntimeError, match="Unable to snapshot Oddish-managed EC2 instances"
        ):
            asyncio.run(backend.snapshot_managed_instances())
    finally:
        backend.remove_materialized_worker_credentials()

    assert "metric=ec2_inventory_error" in caplog.text


def test_ec2_inventory_runs_account_and_pagination_aws_calls_off_event_loop(
    monkeypatch,
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    main_thread_id = threading.get_ident()
    aws_thread_ids: list[int] = []
    paginator = _FakeDescribeInstancesPaginator([{"Reservations": []}])
    original_paginate = paginator._paginate

    def resolve_account(_self) -> str:
        aws_thread_ids.append(threading.get_ident())
        return AWS_ACCOUNT_ID

    def paginate(**kwargs):
        aws_thread_ids.append(threading.get_ident())
        return original_paginate(**kwargs)

    monkeypatch.setattr(Ec2Backend, "_resolve_aws_account_id", resolve_account)
    paginator.paginate.side_effect = paginate
    ec2 = MagicMock()
    ec2.get_paginator.return_value = paginator
    _install_fake_boto3(monkeypatch, ec2=ec2)
    backend = Ec2Backend()

    try:
        asyncio.run(backend.snapshot_managed_instances())
    finally:
        backend.remove_materialized_worker_credentials()

    assert len(aws_thread_ids) == 2
    assert all(thread_id != main_thread_id for thread_id in aws_thread_ids)


def test_ec2_inventory_times_out_and_logs_instead_of_blocking_reconciliation(
    monkeypatch, caplog
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    import oddish.runtime.backends.ec2 as ec2_module

    monkeypatch.setattr(ec2_module, "_EC2_INVENTORY_TIMEOUT_SECONDS", 0.01)

    async def never_finishes(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(ec2_module.asyncio, "to_thread", never_finishes)
    backend = Ec2Backend()

    with caplog.at_level("ERROR"), pytest.raises(
        RuntimeError, match="Unable to snapshot Oddish-managed EC2 instances"
    ) as error:
        asyncio.run(backend.snapshot_managed_instances())

    assert isinstance(error.value.__cause__, TimeoutError)
    assert "metric=ec2_inventory_error" in caplog.text


def test_ec2_inventory_refuses_missing_or_non_datetime_launch_time(
    monkeypatch, caplog
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    monkeypatch.setenv("MODAL_APP_NAME", "oddish-pr-42")
    missing = _inventory_instance("i-missing-time")
    missing.pop("LaunchTime")
    invalid = _inventory_instance("i-invalid-time")
    invalid["LaunchTime"] = "2026-08-07T12:30:00Z"
    paginator = _FakeDescribeInstancesPaginator(
        [
            {
                "Reservations": [
                    {
                        "Instances": [
                            missing,
                            invalid,
                            _inventory_instance("i-valid-time"),
                        ]
                    }
                ]
            }
        ]
    )
    ec2 = MagicMock()
    ec2.get_paginator.return_value = paginator
    _install_fake_boto3(monkeypatch, ec2=ec2)
    backend = Ec2Backend()

    try:
        with caplog.at_level("WARNING"):
            inventory = asyncio.run(backend.snapshot_managed_instances())
    finally:
        backend.remove_materialized_worker_credentials()

    assert [item.instance_id for item in inventory.instances] == ["i-valid-time"]
    assert caplog.text.count("metric=ec2_inventory_launch_time_refusal") == 2


def test_ec2_backend_resolves_the_current_sts_account_on_every_check(
    monkeypatch,
) -> None:
    configured = _complete_ec2_settings()
    import oddish.runtime.backends.ec2 as ec2_module

    monkeypatch.setattr(ec2_module, "settings", configured)
    monkeypatch.setattr(
        Ec2Backend, "_resolve_aws_account_id", _ORIGINAL_RESOLVE_AWS_ACCOUNT_ID
    )
    sts = MagicMock()
    sts.get_caller_identity.side_effect = [
        {"Account": AWS_ACCOUNT_ID},
        {"Account": "999999999999"},
    ]
    client_factory = _install_fake_boto3(monkeypatch, sts=sts)
    backend = Ec2Backend()

    assert backend._resolve_aws_account_id() == AWS_ACCOUNT_ID
    assert backend._resolve_aws_account_id() == "999999999999"

    assert client_factory.call_args_list == [
        ((), {"profile_name": "oddish-ec2", "region_name": "us-east-1"}),
        ((), {"profile_name": "oddish-ec2", "region_name": "us-east-1"}),
    ]
    assert sts.get_caller_identity.call_count == 2


def test_ec2_teardown_rechecks_account_after_credentials_rotate(
    monkeypatch,
) -> None:
    configured = _complete_ec2_settings()
    import oddish.runtime.backends.ec2 as ec2_module

    monkeypatch.setattr(ec2_module, "settings", configured)
    monkeypatch.setattr(
        Ec2Backend, "_resolve_aws_account_id", _ORIGINAL_RESOLVE_AWS_ACCOUNT_ID
    )
    sts = MagicMock()
    sts.get_caller_identity.side_effect = [
        {"Account": AWS_ACCOUNT_ID},
        {"Account": "999999999999"},
    ]
    ec2 = MagicMock()
    client_factory = _install_fake_boto3(monkeypatch, sts=sts, ec2=ec2)
    backend = Ec2Backend()

    assert backend._resolve_aws_account_id() == AWS_ACCOUNT_ID
    assert asyncio.run(backend.teardown(_ec2_handle())) is False

    assert client_factory.call_args_list == [
        ((), {"profile_name": "oddish-ec2", "region_name": "us-east-1"}),
        ((), {"profile_name": "oddish-ec2", "region_name": "us-east-1"}),
    ]
    ec2.describe_instances.assert_not_called()
    ec2.terminate_instances.assert_not_called()


def test_ec2_teardown_rejects_legacy_or_cross_account_handles_before_ec2_call(
    monkeypatch,
) -> None:
    _install_complete_ec2_settings(monkeypatch)
    ec2 = MagicMock()
    client_factory = _install_fake_boto3(monkeypatch, ec2=ec2)
    backend = Ec2Backend()

    assert asyncio.run(backend.teardown("i-legacy")) is False
    assert (
        asyncio.run(backend.teardown(_ec2_handle(account_id="999999999999")))
        is False
    )

    client_factory.assert_not_called()
    ec2.describe_instances.assert_not_called()


def test_ec2_teardown_uses_region_persisted_in_handle(monkeypatch) -> None:
    _install_complete_ec2_settings(monkeypatch, ec2_region="us-east-1")
    ec2 = MagicMock()
    ec2.describe_instances.return_value = _owned_instance_description()
    client_factory = _install_fake_boto3(monkeypatch, ec2=ec2)

    result = asyncio.run(
        Ec2Backend().teardown(_ec2_handle(region="eu-west-1"))
    )

    assert result is True
    client_factory.assert_called_once_with(
        profile_name="oddish-ec2", region_name="eu-west-1"
    )


def test_ec2_harbor_patch_exposes_identity_and_keeps_host_only_imdsv2(
    monkeypatch,
) -> None:
    import oddish.workers.harbor.patches as harbor_patches

    class FakeEc2Environment:
        provider_name = ""
        _oddish_ec2_lifecycle_patched = True

        def __init__(self):
            self.instance_id = "i-123"
            self.region = "us-east-1"
            self.user_tags = {
                MANAGED_TAG_KEY: "true",
                AWS_ACCOUNT_ID_TAG_KEY: "123456789012",
            }

        def get_sandbox_id(self):
            return None

        def _run_instances_kwargs(self):
            return {"ImageId": "ami-123"}

    module = SimpleNamespace(EC2Environment=FakeEc2Environment)
    monkeypatch.setattr(
        harbor_patches.importlib,
        "import_module",
        lambda name: (
            module
            if name == "harbor.environments.ec2"
            else importlib.import_module(name)
        ),
    )

    harbor_patches._patch_ec2_lifecycle()
    environment = FakeEc2Environment()

    assert FakeEc2Environment.provider_name == "ec2"
    assert environment.get_sandbox_id() == (
        "ec2://123456789012/us-east-1/i-123"
    )
    assert environment._run_instances_kwargs()["MetadataOptions"] == {
        "HttpEndpoint": "enabled",
        "HttpTokens": "required",
        "HttpPutResponseHopLimit": 1,
    }

    first_launch_method = FakeEc2Environment._run_instances_kwargs
    harbor_patches._patch_ec2_lifecycle()
    assert FakeEc2Environment._run_instances_kwargs is first_launch_method


@pytest.mark.asyncio
async def test_ec2_harbor_patch_reports_identity_immediately_after_launch(
    monkeypatch,
) -> None:
    import oddish.workers.harbor.patches as harbor_patches

    class FakeEc2Environment:
        def __init__(self):
            self.instance_id = None
            self.region = "us-west-2"
            self.user_tags = {
                MANAGED_TAG_KEY: "true",
                AWS_ACCOUNT_ID_TAG_KEY: "123456789012",
            }

        async def _launch_instance(self):
            return "i-launched"

        def _run_instances_kwargs(self):
            return {}

    module = SimpleNamespace(EC2Environment=FakeEc2Environment)
    monkeypatch.setattr(
        harbor_patches.importlib,
        "import_module",
        lambda _name: module,
    )
    harbor_patches._patch_ec2_lifecycle(require_ec2=True)
    seen = []

    async def launched(environment):
        seen.append(environment.get_sandbox_id())

    token = harbor_patches.set_ec2_instance_launched_callback(launched)
    try:
        environment = FakeEc2Environment()
        instance_id = await environment._launch_instance()
    finally:
        harbor_patches.reset_ec2_instance_launched_callback(token)

    assert instance_id == "i-launched"
    assert environment.instance_id == "i-launched"
    assert seen == ["ec2://123456789012/us-west-2/i-launched"]


@pytest.mark.asyncio
async def test_ec2_harbor_patch_retains_instance_id_when_launch_callback_fails(
    monkeypatch,
) -> None:
    import oddish.workers.harbor.patches as harbor_patches

    class FakeEc2Environment:
        def __init__(self):
            self.instance_id = None

        async def _launch_instance(self):
            return "i-launched"

        def _run_instances_kwargs(self):
            return {}

    module = SimpleNamespace(EC2Environment=FakeEc2Environment)
    monkeypatch.setattr(
        harbor_patches.importlib,
        "import_module",
        lambda _name: module,
    )
    harbor_patches._patch_ec2_lifecycle(require_ec2=True)

    async def fail_after_launch(_environment):
        raise RuntimeError("identity persistence failed")

    token = harbor_patches.set_ec2_instance_launched_callback(fail_after_launch)
    environment = FakeEc2Environment()
    try:
        with pytest.raises(RuntimeError, match="identity persistence failed"):
            await environment._launch_instance()
    finally:
        harbor_patches.reset_ec2_instance_launched_callback(token)

    assert environment.instance_id == "i-launched"


def test_ec2_harbor_patch_preserves_raw_id_for_non_oddish_environments(
    monkeypatch,
) -> None:
    import oddish.workers.harbor.patches as harbor_patches

    class FakeEc2Environment:
        instance_id = "i-plain"
        region = "us-east-1"
        user_tags = {}

        def _run_instances_kwargs(self):
            return {}

    module = SimpleNamespace(EC2Environment=FakeEc2Environment)
    monkeypatch.setattr(
        harbor_patches.importlib,
        "import_module",
        lambda _name: module,
    )

    harbor_patches._patch_ec2_lifecycle()

    assert FakeEc2Environment().get_sandbox_id() == "i-plain"


def test_ec2_harbor_patch_requires_imdsv2_for_platform_instance_profile(
    monkeypatch,
) -> None:
    import oddish.workers.harbor.patches as harbor_patches

    class FakeEc2Environment:
        def _run_instances_kwargs(self):
            return {"IamInstanceProfile": {"Name": "platform-role"}}

    module = SimpleNamespace(EC2Environment=FakeEc2Environment)
    monkeypatch.setattr(
        harbor_patches.importlib,
        "import_module",
        lambda name: (
            module
            if name == "harbor.environments.ec2"
            else importlib.import_module(name)
        ),
    )

    harbor_patches._patch_ec2_lifecycle()

    assert FakeEc2Environment()._run_instances_kwargs()["MetadataOptions"] == {
        "HttpEndpoint": "enabled",
        "HttpTokens": "required",
        "HttpPutResponseHopLimit": 2,
    }


@pytest.mark.parametrize("missing_surface", ["module", "class", "launch_method"])
def test_required_ec2_trial_fails_loudly_when_the_pinned_harbor_surface_is_missing(
    monkeypatch, missing_surface: str
) -> None:
    import oddish.workers.harbor.patches as harbor_patches

    monkeypatch.setenv("ODDISH_EC2_ENABLED", "true")
    if missing_surface == "module":
        def import_module(_name):
            raise ImportError("missing")
    elif missing_surface == "class":
        def import_module(_name):
            return SimpleNamespace()
    else:
        def import_module(_name):
            return SimpleNamespace(EC2Environment=type("EC2Environment", (), {}))
    monkeypatch.setattr(harbor_patches.importlib, "import_module", import_module)

    with pytest.raises(RuntimeError, match="EC2 trial requires"):
        harbor_patches._patch_ec2_lifecycle(require_ec2=True)


def test_enabled_ec2_does_not_break_unrelated_harbor_trial_when_ec2_surface_missing(
    monkeypatch,
) -> None:
    import oddish.workers.harbor.patches as harbor_patches

    monkeypatch.setenv("ODDISH_EC2_ENABLED", "true")
    monkeypatch.setattr(
        harbor_patches.importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("missing")),
    )

    harbor_patches._patch_ec2_lifecycle(require_ec2=False)
