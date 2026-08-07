from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from oddish.config import settings
from oddish.runtime.ec2_policy import (
    AWS_ACCOUNT_ID_TAG_KEY,
    DEPLOYMENT_TAG_KEY,
    MANAGED_TAG_KEY,
    PROTECTED_EC2_KWARGS,
    validate_ec2_user_tags,
)
from oddish.runtime.ports import Capabilities

if TYPE_CHECKING:
    from oddish.runtime.ports import ExecutionBackend

logger = logging.getLogger(__name__)

_EC2_HANDLE_PATTERN = re.compile(
    r"^ec2://(?P<account_id>[0-9]{12})/"
    r"(?P<region>[a-z0-9-]+)/(?P<instance_id>i-[A-Za-z0-9-]+)$"
)

class Ec2Backend:
    name = "ec2"

    def __init__(self) -> None:
        self._ssh_key_path: Path | None = None
        self._cleanup_registered = False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            gpu=None,
            tpu=None,
            private_registry_pull=False,
            network_egress="allow",
            persistent_volumes=False,
            streaming_logs=True,
            memory_snapshot_fork=False,
            cold_start="minutes",
        )

    def materialize_ssh_private_key(self) -> Path:
        if self._ssh_key_path is not None and self._ssh_key_path.exists():
            return self._ssh_key_path
        secret = settings.ec2_ssh_private_key
        if secret is None or not secret.get_secret_value().strip():
            raise RuntimeError("ODDISH_EC2_SSH_PRIVATE_KEY is required for EC2")
        descriptor, raw_path = tempfile.mkstemp(prefix="oddish-ec2-key-")
        path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(secret.get_secret_value().rstrip("\n") + "\n")
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self._ssh_key_path = path
        if not self._cleanup_registered:
            atexit.register(self.remove_materialized_ssh_private_key)
            self._cleanup_registered = True
        return path

    def remove_materialized_ssh_private_key(self) -> None:
        if self._ssh_key_path is None:
            return
        self._ssh_key_path.unlink(missing_ok=True)
        self._ssh_key_path = None

    def harbor_env_kwargs(self, base_kwargs: dict[str, Any]) -> dict[str, Any]:
        protected_overrides = sorted(PROTECTED_EC2_KWARGS.intersection(base_kwargs))
        if protected_overrides:
            raise ValueError(
                "EC2 environment kwargs cannot override platform-owned settings: "
                + ", ".join(protected_overrides)
            )
        raw_tags = (
            validate_ec2_user_tags(base_kwargs["tags"])
            if "tags" in base_kwargs
            else {}
        )
        passthrough = {
            key: value for key, value in base_kwargs.items() if key != "tags"
        }
        tags = {
            **raw_tags,
            MANAGED_TAG_KEY: "true",
            DEPLOYMENT_TAG_KEY: self._deployment_name(),
            AWS_ACCOUNT_ID_TAG_KEY: self._resolve_aws_account_id(),
        }
        return {
            **passthrough,
            "region": settings.ec2_region,
            "ami_id": settings.ec2_ami_id,
            "instance_type": settings.ec2_instance_type,
            "subnet_id": settings.ec2_subnet_id,
            "security_group_ids": list(settings.ec2_security_group_ids),
            "key_name": settings.ec2_key_name,
            "ssh_key_path": str(self.materialize_ssh_private_key()),
            "ssh_user": settings.ec2_ssh_user,
            "launch_mode": "ephemeral",
            "use_public_ip": settings.ec2_use_public_ip,
            "root_volume_size_gb": settings.ec2_root_volume_size_gb,
            "bootstrap_docker": settings.ec2_bootstrap_docker,
            "tags": tags,
        }

    async def teardown(self, external_id: str) -> bool:
        if not external_id:
            return False
        match = _EC2_HANDLE_PATTERN.fullmatch(external_id)
        if match is None:
            logger.error(
                "Ec2Backend.teardown: refusing malformed or legacy EC2 handle %r; "
                "expected ec2://<account>/<region>/<instance>",
                external_id,
            )
            return False
        account_id = match.group("account_id")
        region = match.group("region")
        instance_id = match.group("instance_id")
        try:
            import boto3

            current_account_id = self._resolve_aws_account_id()
            if current_account_id != account_id:
                logger.error(
                    "Ec2Backend.teardown: refusing handle for AWS account %s; "
                    "current credentials are for %s",
                    account_id,
                    current_account_id,
                )
                return False
            client = boto3.client("ec2", region_name=region)
            response = await asyncio.to_thread(
                client.describe_instances, InstanceIds=[instance_id]
            )
            instances = [
                instance
                for reservation in response.get("Reservations", [])
                for instance in reservation.get("Instances", [])
                if instance.get("InstanceId") == instance_id
            ]
            if len(instances) != 1:
                logger.warning(
                    "Ec2Backend.teardown: refusing missing or ambiguous instance %s",
                    instance_id,
                )
                return False
            tags = {
                tag.get("Key"): tag.get("Value")
                for tag in instances[0].get("Tags", [])
                if tag.get("Key")
            }
            expected = {
                MANAGED_TAG_KEY: "true",
                DEPLOYMENT_TAG_KEY: self._deployment_name(),
                AWS_ACCOUNT_ID_TAG_KEY: account_id,
            }
            if any(tags.get(key) != value for key, value in expected.items()):
                logger.warning(
                    "Ec2Backend.teardown: refusing instance %s without matching "
                    "ownership tags",
                    instance_id,
                )
                return False
            await asyncio.to_thread(
                client.terminate_instances, InstanceIds=[instance_id]
            )
        except Exception:
            logger.exception("Ec2Backend.teardown: failed to terminate %s", external_id)
            return False
        logger.info("Ec2Backend.teardown: terminated %s", instance_id)
        return True

    def _resolve_aws_account_id(self) -> str:
        try:
            import boto3

            response = boto3.client(
                "sts", region_name=settings.ec2_region
            ).get_caller_identity()
        except Exception as exc:
            raise RuntimeError(
                "Unable to resolve the AWS account for the EC2 backend"
            ) from exc
        account_id = str(response.get("Account") or "").strip()
        if not re.fullmatch(r"[0-9]{12}", account_id):
            raise RuntimeError(
                "STS GetCallerIdentity returned an invalid AWS account ID"
            )
        return account_id

    @contextlib.contextmanager
    def capture_diagnostics(self, job_dir: Path) -> Iterator[Path | None]:
        yield None

    @staticmethod
    def _deployment_name() -> str:
        return os.environ.get("MODAL_APP_NAME", "oddish")


if TYPE_CHECKING:
    _: ExecutionBackend = Ec2Backend()
