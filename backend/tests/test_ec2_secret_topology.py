from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import modal_app
import worker.functions as worker_functions

CONTROL = "oddish-ec2-control"
SSH = "oddish-ec2-ssh"


def test_ec2_image_env_allowlist_contains_only_non_secret_runtime_settings() -> None:
    expected = {
        "ODDISH_EC2_ENABLED",
        "ODDISH_EC2_REGION",
        "ODDISH_EC2_AMI_ID",
        "ODDISH_EC2_INSTANCE_TYPE",
        "ODDISH_EC2_SUBNET_ID",
        "ODDISH_EC2_SECURITY_GROUP_IDS",
        "ODDISH_EC2_KEY_NAME",
        "ODDISH_EC2_SSH_USER",
        "ODDISH_EC2_ROOT_VOLUME_SIZE_GB",
        "ODDISH_EC2_USE_PUBLIC_IP",
        "ODDISH_EC2_BOOTSTRAP_DOCKER",
    }

    assert modal_app._EC2_PUBLIC_ENV_NAMES == expected
    assert not {
        "ODDISH_EC2_SSH_PRIVATE_KEY",
        "ODDISH_EC2_AWS_ACCESS_KEY_ID",
        "ODDISH_EC2_AWS_SECRET_ACCESS_KEY",
        "ODDISH_EC2_AWS_SESSION_TOKEN",
        "ODDISH_EC2_CONTROL_SECRET_NAME",
        "ODDISH_EC2_SSH_SECRET_NAME",
    }.intersection(modal_app._EC2_PUBLIC_ENV_NAMES)


def _enabled_env() -> dict[str, str]:
    return {
        "ODDISH_EC2_ENABLED": "true",
        "ODDISH_EC2_CONTROL_SECRET_NAME": CONTROL,
        "ODDISH_EC2_SSH_SECRET_NAME": SSH,
    }


def test_disabled_deploy_has_no_ec2_secret_dependencies() -> None:
    assert modal_app._ec2_secret_plan({}, {}) == modal_app.Ec2SecretPlan()


def test_enabled_deploy_requires_separate_control_and_ssh_secrets() -> None:
    plan = modal_app._ec2_secret_plan(_enabled_env(), {})

    assert plan.control_name == CONTROL
    assert plan.ssh_name == SSH
    assert plan.worker_names == (CONTROL, SSH)


@pytest.mark.parametrize(
    "missing",
    ["ODDISH_EC2_CONTROL_SECRET_NAME", "ODDISH_EC2_SSH_SECRET_NAME"],
)
def test_enabled_deploy_fails_loudly_when_a_secret_name_is_missing(
    missing: str,
) -> None:
    environ = _enabled_env()
    environ.pop(missing)

    with pytest.raises(RuntimeError, match=missing):
        modal_app._ec2_secret_plan(environ, {})


def test_enabled_deploy_rejects_one_secret_used_for_both_roles() -> None:
    environ = _enabled_env()
    environ["ODDISH_EC2_SSH_SECRET_NAME"] = CONTROL

    with pytest.raises(RuntimeError, match="separate"):
        modal_app._ec2_secret_plan(environ, {})


def test_ec2_secrets_cannot_alias_a_broad_runtime_secret() -> None:
    plan = modal_app.Ec2SecretPlan(
        control_name="aws-credentials", ssh_name=SSH
    )

    with pytest.raises(RuntimeError, match="broad runtime"):
        modal_app._validate_ec2_secret_isolation(
            plan, {"oddish-prod", "aws-credentials"}
        )


def test_ec2_secret_plan_reads_dotenv_and_process_env_wins() -> None:
    dotenv = _enabled_env()
    assert modal_app._ec2_secret_plan({}, dotenv).worker_names == (CONTROL, SSH)
    assert modal_app._ec2_secret_plan(
        {"ODDISH_EC2_ENABLED": "false"}, dotenv
    ) == modal_app.Ec2SecretPlan()


def test_local_plan_fails_when_effective_enablement_disagrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: True)
    monkeypatch.setattr(
        modal_app,
        "_ec2_secret_plan",
        lambda _environ, _dotenv: modal_app.Ec2SecretPlan(),
    )

    with pytest.raises(RuntimeError, match="enabledness disagrees"):
        modal_app._resolve_ec2_secret_plan(_enabled_env(), {})


def test_container_plan_rejects_runtime_disablement_of_enabled_baked_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: False)
    plan_file = tmp_path / "ec2-secret-plan.json"
    plan_file.write_text(
        json.dumps({"control_name": CONTROL, "ssh_name": SSH})
    )
    monkeypatch.setattr(modal_app, "_EC2_PLAN_FILE", str(plan_file))

    polluted = {
        "ODDISH_EC2_ENABLED": "false",
        "ODDISH_EC2_CONTROL_SECRET_NAME": "attacker-control",
        "ODDISH_EC2_SSH_SECRET_NAME": "attacker-ssh",
    }
    with pytest.raises(RuntimeError, match="enabledness disagrees"):
        modal_app._resolve_ec2_secret_plan(polluted, {})


def test_container_plan_rejects_runtime_enablement_of_disabled_baked_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: False)
    plan_file = tmp_path / "ec2-secret-plan.json"
    plan_file.write_text(json.dumps({"control_name": None, "ssh_name": None}))
    monkeypatch.setattr(modal_app, "_EC2_PLAN_FILE", str(plan_file))

    with pytest.raises(RuntimeError, match="enabledness disagrees"):
        modal_app._resolve_ec2_secret_plan(_enabled_env(), {})


@pytest.mark.parametrize(
    "runtime_env,plan_payload",
    [
        (
            _enabled_env(),
            {"control_name": CONTROL, "ssh_name": SSH},
        ),
        (
            {"ODDISH_EC2_ENABLED": "false"},
            {"control_name": None, "ssh_name": None},
        ),
    ],
)
def test_container_plan_accepts_matching_runtime_enablement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    runtime_env: dict[str, str],
    plan_payload: dict[str, str | None],
) -> None:
    monkeypatch.setattr(modal_app.modal, "is_local", lambda: False)
    plan_file = tmp_path / "ec2-secret-plan.json"
    plan_file.write_text(json.dumps(plan_payload))
    monkeypatch.setattr(modal_app, "_EC2_PLAN_FILE", str(plan_file))

    plan = modal_app._resolve_ec2_secret_plan(runtime_env, {})

    assert bool(plan.worker_names) is (runtime_env["ODDISH_EC2_ENABLED"] == "true")


@pytest.mark.parametrize(
    "secret_name",
    [
        "ODDISH_EC2_SSH_PRIVATE_KEY",
        "ODDISH_EC2_AWS_ACCESS_KEY_ID",
        "ODDISH_EC2_AWS_SECRET_ACCESS_KEY",
        "ODDISH_EC2_AWS_SESSION_TOKEN",
    ],
)
def test_backend_dotenv_rejects_raw_ec2_secret_values(secret_name: str) -> None:
    with pytest.raises(RuntimeError, match=secret_name):
        modal_app._validate_ec2_dotenv_secret_isolation({secret_name: "secret"})


def test_runtime_secret_base_list_is_not_mutated_with_ec2_secrets() -> None:
    assert all(
        secret not in modal_app.runtime_secrets
        for secret in (*modal_app.ec2_control_secrets, *modal_app.ec2_worker_secrets)
    )


def test_enabled_import_builds_scoped_secret_objects_without_mutating_base() -> None:
    code = """
import json
import modal_app
print(json.dumps({
    "control": len(modal_app.ec2_control_secrets),
    "worker": len(modal_app.ec2_worker_secrets),
    "base_overlap": any(
        secret in modal_app.runtime_secrets
        for secret in modal_app.ec2_worker_secrets
    ),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(modal_app.__file__).parent,
        env={
            **os.environ,
            "ODDISH_EC2_ENABLED": "true",
            "ODDISH_EC2_CONTROL_SECRET_NAME": CONTROL,
            "ODDISH_EC2_SSH_SECRET_NAME": SSH,
            "ODDISH_SAURON_AWS_SECRET_NAME": "",
            "ODDISH_EC2_REGION": "us-east-1",
            "ODDISH_EC2_AMI_ID": "ami-test",
            "ODDISH_EC2_SUBNET_ID": "subnet-test",
            "ODDISH_EC2_SECURITY_GROUP_IDS": '["sg-test"]',
            "ODDISH_EC2_KEY_NAME": "oddish-test",
        },
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout.splitlines()[-1]) == {
        "control": 1,
        "worker": 2,
        "base_overlap": False,
    }


def test_only_trial_workers_and_reconciler_receive_ec2_secrets() -> None:
    source = inspect.getsource(worker_functions)

    assert "secrets=trial_worker_secrets" in source
    assert "secrets=reconciler_secrets" in source
    assert "secrets=runtime_secrets" in source
    assert "secrets=ec2_worker_secrets" not in source
    assert "secrets=ec2_control_secrets" not in source

    endpoints_source = Path(modal_app.__file__).with_name("endpoints.py").read_text()
    assert "runtime_secrets" in endpoints_source
    assert "trial_worker_secrets" not in endpoints_source
    assert "reconciler_secrets" not in endpoints_source
    assert "ec2_control_secrets" not in endpoints_source
    assert 'register_provider_teardown_delegate("ec2"' in endpoints_source
    assert "teardown_ec2_sandbox" in source


def test_default_and_variant_workers_share_the_same_ec2_secret_topology() -> None:
    assert worker_functions.trial_worker_secrets == [
        *modal_app.runtime_secrets,
        *modal_app.ec2_worker_secrets,
    ]
    assert worker_functions.reconciler_secrets == [
        *modal_app.runtime_secrets,
        *modal_app.ec2_control_secrets,
    ]
    assert all(
        secret not in worker_functions.reconciler_secrets
        for secret in modal_app.ec2_ssh_secrets
    )
