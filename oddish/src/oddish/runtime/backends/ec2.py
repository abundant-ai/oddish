from __future__ import annotations

import asyncio
import atexit
import contextlib
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator

from oddish.config import settings
from oddish.runtime.ec2_policy import (
    AWS_ACCOUNT_ID_TAG_KEY,
    DEPLOYMENT_TAG_KEY,
    MANAGED_TAG_KEY,
    PROTECTED_EC2_KWARGS,
    TRIAL_ID_TAG_KEY,
    WORKER_JOB_ID_TAG_KEY,
    validate_ec2_user_tags,
)
from oddish.runtime.ports import Capabilities

if TYPE_CHECKING:
    from oddish.runtime.ec2_orphans import Ec2InventorySnapshot
    from oddish.runtime.ports import ExecutionBackend

logger = logging.getLogger(__name__)

_EC2_HANDLE_PATTERN = re.compile(
    r"^ec2://(?P<account_id>[0-9]{12})/"
    r"(?P<region>[a-z0-9-]+)/(?P<instance_id>i-[A-Za-z0-9-]+)$"
)
_AWS_PROFILE_NAME = "oddish-ec2"
_AWS_SHARED_CREDENTIALS_FILE = "AWS_SHARED_CREDENTIALS_FILE"
_EC2_INVENTORY_TIMEOUT_SECONDS = 60.0
_EC2_INVENTORY_STATES = (
    "pending",
    "running",
    "stopping",
    "stopped",
    "shutting-down",
)


class Ec2Backend:
    name = "ec2"

    def __init__(self) -> None:
        self._ssh_key_path: Path | None = None
        self._aws_profile_path: Path | None = None
        self._previous_aws_credentials_file: str | None = None
        self._had_aws_credentials_file = False
        self._aws_profile_replacement_active = False
        self._cleanup_registered = False
        self._credential_lock = threading.RLock()
        self._credential_leases = 0

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
        with self._credential_lock:
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
        with self._credential_lock:
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
                    if (
                        session_token is not None
                        and session_token.get_secret_value().strip()
                    ):
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
            self._aws_profile_replacement_active = True
            self._register_cleanup()
            return path

    def acquire_worker_credentials(self, *, include_ssh: bool) -> None:
        with self._credential_lock:
            try:
                self.materialize_aws_profile()
                if include_ssh:
                    self.materialize_ssh_private_key()
            except Exception:
                if self._credential_leases == 0:
                    self._remove_materialized_worker_credentials_unlocked()
                raise
            self._credential_leases += 1

    def release_worker_credentials(self) -> None:
        with self._credential_lock:
            if self._credential_leases <= 0:
                raise RuntimeError("EC2 credential lease released without acquisition")
            self._credential_leases -= 1
            if self._credential_leases == 0:
                self._remove_materialized_worker_credentials_unlocked()

    def _register_cleanup(self) -> None:
        if not self._cleanup_registered:
            atexit.register(self._force_remove_materialized_worker_credentials)
            self._cleanup_registered = True

    def remove_materialized_ssh_private_key(self) -> None:
        if self._ssh_key_path is None:
            return
        self._ssh_key_path.unlink(missing_ok=True)
        self._ssh_key_path = None

    def remove_materialized_worker_credentials(self) -> None:
        with self._credential_lock:
            if self._credential_leases:
                raise RuntimeError(
                    "Cannot remove EC2 worker credentials while leases are active"
                )
            self._remove_materialized_worker_credentials_unlocked()

    def _force_remove_materialized_worker_credentials(self) -> None:
        with self._credential_lock:
            self._credential_leases = 0
            self._remove_materialized_worker_credentials_unlocked()

    def _remove_materialized_worker_credentials_unlocked(self) -> None:
        self.remove_materialized_ssh_private_key()
        if not self._aws_profile_replacement_active:
            return
        try:
            if self._aws_profile_path is not None:
                self._aws_profile_path.unlink(missing_ok=True)
        finally:
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
            self._aws_profile_replacement_active = False

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
            DEPLOYMENT_TAG_KEY: self.deployment_name(),
            AWS_ACCOUNT_ID_TAG_KEY: self.resolve_aws_account_id(),
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
            "iam_instance_profile": settings.ec2_instance_profile,
            "bootstrap_docker": settings.ec2_bootstrap_docker,
            "tags": tags,
        }

    async def teardown(self, external_id: str) -> bool:
        if not external_id:
            logger.warning(
                "metric=ec2_teardown outcome=refused reason=empty_external_id"
            )
            return False
        self.acquire_worker_credentials(include_ssh=False)
        try:
            return await self._teardown_with_credentials(external_id)
        finally:
            self.release_worker_credentials()

    async def _teardown_with_credentials(self, external_id: str) -> bool:
        if not external_id:
            logger.warning(
                "metric=ec2_teardown outcome=refused reason=empty_external_id"
            )
            return False
        match = _EC2_HANDLE_PATTERN.fullmatch(external_id)
        if match is None:
            logger.error(
                "metric=ec2_teardown outcome=refused reason=malformed_handle "
                "external_id=%r expected=ec2://<account>/<region>/<instance>",
                external_id,
            )
            return False
        account_id = match.group("account_id")
        region = match.group("region")
        instance_id = match.group("instance_id")
        try:
            import boto3

            current_account_id = await asyncio.to_thread(self.resolve_aws_account_id)
            if current_account_id != account_id:
                logger.error(
                    "metric=ec2_teardown outcome=refused reason=account_mismatch "
                    "handle_account_id=%s current_account_id=%s",
                    account_id,
                    current_account_id,
                )
                return False
            self.materialize_aws_profile()
            client = boto3.Session(
                profile_name=_AWS_PROFILE_NAME, region_name=region
            ).client("ec2", config=_aws_control_client_config())
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
                    "metric=ec2_teardown outcome=refused "
                    "reason=missing_or_ambiguous instance_id=%s",
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
                DEPLOYMENT_TAG_KEY: self.deployment_name(),
                AWS_ACCOUNT_ID_TAG_KEY: account_id,
            }
            if any(tags.get(key) != value for key, value in expected.items()):
                logger.warning(
                    "metric=ec2_teardown outcome=refused "
                    "reason=ownership_tags instance_id=%s",
                    instance_id,
                )
                return False
            state = str((instances[0].get("State") or {}).get("Name") or "")
            if state in {"shutting-down", "terminated"}:
                logger.info(
                    "metric=ec2_teardown outcome=already_terminal "
                    "instance_id=%s state=%s",
                    instance_id,
                    state,
                )
                return True
            await asyncio.to_thread(
                client.terminate_instances, InstanceIds=[instance_id]
            )
        except Exception:
            logger.exception(
                "metric=ec2_teardown outcome=error external_id=%s", external_id
            )
            return False
        logger.info(
            "metric=ec2_teardown outcome=terminated instance_id=%s", instance_id
        )
        return True

    async def snapshot_managed_instances(self) -> Ec2InventorySnapshot:
        """List active instances owned by this Oddish deployment."""
        from oddish.runtime.ec2_orphans import (
            Ec2InstanceSnapshot,
            Ec2InventorySnapshot,
        )

        deployment = self.deployment_name()
        region = settings.ec2_region
        try:
            def _list_inventory() -> tuple[str, list[dict[str, Any]]]:
                self.materialize_aws_profile()
                account_id = self.resolve_aws_account_id()
                import boto3

                client = boto3.Session(
                    profile_name=_AWS_PROFILE_NAME, region_name=region
                ).client("ec2", config=_aws_control_client_config())
                paginator = client.get_paginator("describe_instances")
                pages = list(
                    paginator.paginate(
                        Filters=[
                            {
                                "Name": f"tag:{MANAGED_TAG_KEY}",
                                "Values": ["true"],
                            },
                            {
                                "Name": f"tag:{DEPLOYMENT_TAG_KEY}",
                                "Values": [deployment],
                            },
                            {
                                "Name": "instance-state-name",
                                "Values": list(_EC2_INVENTORY_STATES),
                            },
                        ]
                    )
                )
                return account_id, pages

            account_id, pages = await asyncio.wait_for(
                asyncio.to_thread(_list_inventory),
                timeout=_EC2_INVENTORY_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.exception(
                "metric=ec2_inventory_error deployment=%s region=%s",
                deployment,
                region,
            )
            raise RuntimeError(
                "Unable to snapshot Oddish-managed EC2 instances"
            ) from exc

        snapshots: list[Ec2InstanceSnapshot] = []
        expected_tags = {
            MANAGED_TAG_KEY: "true",
            DEPLOYMENT_TAG_KEY: deployment,
            AWS_ACCOUNT_ID_TAG_KEY: account_id,
        }
        for page in pages:
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    instance_id = str(instance.get("InstanceId") or "").strip()
                    tags = {
                        str(tag.get("Key")): str(tag.get("Value"))
                        for tag in instance.get("Tags", [])
                        if tag.get("Key") is not None and tag.get("Value") is not None
                    }
                    if any(
                        tags.get(key) != value
                        for key, value in expected_tags.items()
                    ):
                        logger.warning(
                            "metric=ec2_inventory_ownership_refusal "
                            "instance_id=%s deployment=%s region=%s",
                            instance_id or "missing",
                            deployment,
                            region,
                        )
                        continue
                    state = (
                        str((instance.get("State") or {}).get("Name") or "")
                        .strip()
                        .lower()
                    )
                    if state not in _EC2_INVENTORY_STATES:
                        logger.warning(
                            "metric=ec2_inventory_state_refusal "
                            "instance_id=%s state=%s",
                            instance_id or "missing",
                            state or "missing",
                        )
                        continue
                    if not _EC2_HANDLE_PATTERN.fullmatch(
                        f"ec2://{account_id}/{region}/{instance_id}"
                    ):
                        logger.warning(
                            "metric=ec2_inventory_instance_refusal "
                            "reason=invalid_instance_id instance_id=%r",
                            instance_id,
                        )
                        continue
                    raw_launch_time = instance.get("LaunchTime")
                    launch_time = _normalize_launch_time(raw_launch_time)
                    if launch_time is None:
                        logger.warning(
                            "metric=ec2_inventory_launch_time_refusal "
                            "instance_id=%s reason=%s",
                            instance_id,
                            (
                                "missing"
                                if raw_launch_time is None
                                else "invalid_type"
                            ),
                        )
                        continue
                    snapshots.append(
                        Ec2InstanceSnapshot(
                            instance_id=instance_id,
                            region=region,
                            state=state,
                            launch_time=launch_time,
                            managed_tag=tags.get(MANAGED_TAG_KEY),
                            deployment_tag=tags.get(DEPLOYMENT_TAG_KEY),
                            account_id_tag=tags.get(AWS_ACCOUNT_ID_TAG_KEY),
                            trial_id_tag=tags.get(TRIAL_ID_TAG_KEY),
                            worker_job_id_tag=tags.get(WORKER_JOB_ID_TAG_KEY),
                        )
                    )
        logger.info(
            "metric=ec2_inventory_snapshot outcome=success deployment=%s "
            "account_id=%s region=%s instances=%d",
            deployment,
            account_id,
            region,
            len(snapshots),
        )
        return Ec2InventorySnapshot(
            expected_account_id=account_id,
            expected_deployment=deployment,
            instances=tuple(snapshots),
        )

    def resolve_aws_account_id(self) -> str:
        """Resolve the account used by the namespaced EC2 control profile."""
        return self._resolve_aws_account_id()

    def deployment_name(self) -> str:
        """Return the deployment ownership namespace used in protected tags."""
        return self._deployment_name()

    def _resolve_aws_account_id(self) -> str:
        try:
            import boto3

            self.materialize_aws_profile()
            response = boto3.Session(
                profile_name=_AWS_PROFILE_NAME,
                region_name=settings.ec2_region,
            ).client(
                "sts", config=_aws_control_client_config()
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


def _normalize_launch_time(value: Any) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _aws_control_client_config() -> Any:
    from botocore.config import Config

    return Config(
        connect_timeout=5,
        read_timeout=10,
        retries={"max_attempts": 3, "mode": "standard"},
    )


if TYPE_CHECKING:
    _: ExecutionBackend = Ec2Backend()
