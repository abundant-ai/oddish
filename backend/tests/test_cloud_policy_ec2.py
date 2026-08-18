"""Hosted EC2 availability follows the opt-in runtime registry."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_ODDISH_SRC = _BACKEND_ROOT.parent / "oddish" / "src"
_EC2_ENV_NAMES = {
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


def _cloud_policy_values(*, ec2_enabled: bool) -> tuple[set[str], str, str]:
    code = (
        "import cloud_policy;"
        "from fastapi import HTTPException;"
        "from oddish.core.sweeps import build_trial_specs_from_sweep;"
        "from oddish.schemas import TaskSweepSubmission;"
        "print(','.join(sorted(e.value for e in "
        "cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS)));"
        "print(cloud_policy.get_default_cloud_environment().value);"
        "submission=TaskSweepSubmission.model_validate({"
        "'task_id':'t','configs':[{'agent':'nop','n_trials':1}],"
        "'environment':'ec2'});"
        "decision='accepted';"
        "\ntry:\n"
        " build_trial_specs_from_sweep(submission,allowed_environments="
        "cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS)\n"
        "except HTTPException as exc:\n decision=f'rejected:{exc.status_code}'\n"
        "print(decision)"
    )
    env = {key: value for key, value in os.environ.items() if key not in _EC2_ENV_NAMES}
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_ODDISH_SRC), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    if ec2_enabled:
        env.update(
            {
                "ODDISH_EC2_ENABLED": "true",
                "ODDISH_EC2_REGION": "us-east-1",
                "ODDISH_EC2_AMI_ID": "ami-123",
                "ODDISH_EC2_SUBNET_ID": "subnet-123",
                "ODDISH_EC2_SECURITY_GROUP_IDS": '["sg-123"]',
                "ODDISH_EC2_KEY_NAME": "oddish-harbor",
            }
        )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_BACKEND_ROOT),
    )
    assert result.returncode == 0, result.stderr
    allowed, default, decision = result.stdout.strip().splitlines()
    return set(allowed.split(",")), default, decision


def test_ec2_is_allowed_only_when_enabled_and_daytona_remains_default() -> None:
    disabled_allowed, disabled_default, disabled_decision = _cloud_policy_values(
        ec2_enabled=False
    )
    enabled_allowed, enabled_default, enabled_decision = _cloud_policy_values(
        ec2_enabled=True
    )

    assert "ec2" not in disabled_allowed
    assert "ec2" in enabled_allowed
    assert disabled_default == "daytona"
    assert enabled_default == "daytona"
    assert disabled_decision == "rejected:400"
    assert enabled_decision == "accepted"
