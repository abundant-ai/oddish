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
_AWS_PROFILE_NAME = "oddish-ec2"
_AWS_SHARED_CREDENTIALS_FILE = "AWS_SHARED_CREDENTIALS_FILE"

class Ec2Backend:
    name = "ec2"

    def __init__(self) -> None:
        self._ssh_key_path: Path | None = None
        self._aws_profile_path: Path | None = None
        self._previous_aws_credentials_file: str | None = None
        self._had_aws_credentials_file = False
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
        self._register_cleanup()
        return path

    def materialize_aws_profile(self) -> Path:
        if self._aws_profile_path is not None and self._aws_profile_path.exists():
            return self._aws_profile_path
        access_key = settings.ec2_aws_access_key_id
        secret_key = settings.ec2_aws_secret_access_key
        if access_key is None or not access_key.get_secret_value().strip():
            raise RuntimeError("ODDISH_EC2_AWS_ACCESS_KEY_ID is required for EC2")
        if secret_key is None or not secret_key.get_secret_value().strip():
            raise RuntimeError("ODDISH_EC2_AWS_SECRET_ACCESS_KEY is required for EC2")
        descriptor, raw_path = tempfile.mkstemp(prefix="oddish-ec2-aws-")
        path = Path(raw_path)
        session_token = settings.ec2_aws_session_token
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(f"[{_AWS_PROFILE_NAME}]\n")
                handle.write(
                    "aws_access_key_id = "
                    + access_key.get_secret_value().strip()
                    + "\n"
                )
                handle.write(
                    "aws_secret_access_key = "
                    + secret_key.get_secret_value().strip()
                    + "\n"
                )
                if session_token is not None and session_token.get_secret_value().strip():
                    handle.write(
                        "aws_session_token = "
                        + session_token.get_secret_value().strip()
                        + "\n"
                    )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        self._had_aws_credentials_file = _AWS_SHARED_CREDENTIALS_FILE in os.environ
        self._previous_aws_credentials_file = os.environ.get(
            _AWS_SHARED_CREDENTIALS_FILE
        )
        os.environ[_AWS_SHARED_CREDENTIALS_FILE] = str(path)
        self._aws_profile_path = path
        self._register_cleanup()
        return path

    def _register_cleanup(self) -> None:
        if not self._cleanup_registered:
            atexit.register(self.remove_materialized_worker_credentials)
            self._cleanup_registered = True

    def remove_materialized_ssh_private_key(self) -> None:
        if self._ssh_key_path is None:
            return
        self._ssh_key_path.unlink(missing_ok=True)
        self._ssh_key_path = None

    def remove_materialized_worker_credentials(self) -> None:
        self.remove_materialized_ssh_private_key()
        if self._aws_profile_path is not None:
            self._aws_profile_path.unlink(missing_ok=True)
            self._aws_profile_path = None
        if self._had_aws_credentials_file:
            assert self._previous_aws_credentials_file is not None
            os.environ[_AWS_SHARED_CREDENTIALS_FILE] = (
                self._previous_aws_credentials_file
            )
        else:
            os.environ.pop(_AWS_SHARED_CREDENTIALS_FILE, None)
        self._previous_aws_credentials_file = None
        self._had_aws_credentials_file = False

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
        self.materialize_aws_profile()
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
            "aws_profile": _AWS_PROFILE_NAME,
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
            self.materialize_aws_profile()
            client = boto3.Session(
                profile_name=_AWS_PROFILE_NAME, region_name=region
            ).client("ec2")
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
            state = str((instances[0].get("State") or {}).get("Name") or "")
            if state in {"shutting-down", "terminated"}:
                logger.info(
                    "Ec2Backend.teardown: instance %s is already %s",
                    instance_id,
                    state,
                )
                return True
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

            self.materialize_aws_profile()
            response = boto3.Session(
                profile_name=_AWS_PROFILE_NAME,
                region_name=settings.ec2_region,
            ).client("sts").get_caller_identity()
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
