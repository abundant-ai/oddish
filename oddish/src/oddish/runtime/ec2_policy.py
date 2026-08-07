from __future__ import annotations

from collections.abc import Mapping
from typing import Any

NAME_TAG_KEY = "Name"
HARBOR_ENVIRONMENT_TAG_KEY = "harbor:environment"
HARBOR_SESSION_TAG_KEY = "harbor:session"
HARBOR_TASK_TAG_KEY = "harbor:task"
MANAGED_TAG_KEY = "oddish:managed"
DEPLOYMENT_TAG_KEY = "oddish:deployment"
TRIAL_ID_TAG_KEY = "oddish:trial-id"
WORKER_JOB_ID_TAG_KEY = "oddish:worker-job-id"
AWS_ACCOUNT_ID_TAG_KEY = "oddish:aws-account-id"

PROTECTED_EC2_TAGS = frozenset(
    {
        NAME_TAG_KEY,
        HARBOR_ENVIRONMENT_TAG_KEY,
        HARBOR_SESSION_TAG_KEY,
        HARBOR_TASK_TAG_KEY,
        MANAGED_TAG_KEY,
        DEPLOYMENT_TAG_KEY,
        TRIAL_ID_TAG_KEY,
        WORKER_JOB_ID_TAG_KEY,
        AWS_ACCOUNT_ID_TAG_KEY,
    }
)

PROTECTED_EC2_KWARGS = frozenset(
    {
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
    }
)


def validate_ec2_user_tags(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("EC2 environment kwarg 'tags' must be a mapping")
    protected = sorted(PROTECTED_EC2_TAGS.intersection(value))
    if protected:
        raise ValueError(
            "EC2 tags cannot override protected platform tags: "
            + ", ".join(protected)
        )
    return dict(value)


def validate_ec2_environment_config(environment_config: Any) -> None:
    if not environment_config.delete:
        raise ValueError(
            "EC2 trials require environment.delete=true; delete=false is unsupported"
        )
    if (environment_config.override_gpus or 0) > 0:
        raise ValueError("EC2 v1 does not support GPU requests")
    if environment_config.override_tpu is not None:
        raise ValueError("EC2 v1 does not support TPU requests")
    protected = sorted(
        PROTECTED_EC2_KWARGS.intersection(environment_config.kwargs)
    )
    if protected:
        raise ValueError(
            "EC2 environment kwargs cannot override platform-owned settings: "
            + ", ".join(protected)
        )
    if "tags" in environment_config.kwargs:
        validate_ec2_user_tags(environment_config.kwargs["tags"])
