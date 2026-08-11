"""Durable, race-safe lifecycle operations for remote sandbox attempts."""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, cast

from sqlalchemy import select

from oddish.db import (
    SandboxRunModel,
    SandboxRunState,
    WorkerJobModel,
    WorkerJobStatus,
    generate_id,
    get_session,
)
from oddish.runtime.ec2_policy import Ec2SandboxOwnership


logger = logging.getLogger(__name__)

DEFAULT_EXECUTION_LANE = "default"
EC2_TRIAL_EXECUTION_LANE = "ec2_trial"

_EC2_HANDLE = re.compile(
    r"^ec2://(?P<account>[0-9]{12})/(?P<region>[a-z0-9-]+)/"
    r"(?P<instance>i-[A-Za-z0-9-]+)$"
)


class SandboxLifecycleError(RuntimeError):
    pass


class SandboxProvisioningRefused(SandboxLifecycleError):
    pass


class _Ec2LifecycleBackend(Protocol):
    def deployment_name(self) -> str: ...

    def configured_region(self) -> str: ...

    async def resolve_aws_account_id_async(self) -> str: ...

    async def teardown_owned(
        self, external_id: str, *, ownership: Ec2SandboxOwnership
    ) -> bool: ...


@dataclass(frozen=True)
class SandboxLaunchContext:
    sandbox_run_id: str
    worker_job_id: str
    worker_job_attempt: int
    trial_id: str
    launch_token: str
    deployment: str
    aws_account_id: str
    region: str

    def ownership(self, external_id: str) -> Ec2SandboxOwnership:
        return Ec2SandboxOwnership(
            deployment=self.deployment,
            aws_account_id=self.aws_account_id,
            region=self.region,
            sandbox_run_id=self.sandbox_run_id,
            worker_job_id=self.worker_job_id,
            worker_job_attempt=self.worker_job_attempt,
            launch_token=self.launch_token,
            trial_id=self.trial_id,
            external_id=external_id,
        )


def execution_lane_for_environment(environment: str | None) -> str:
    return (
        EC2_TRIAL_EXECUTION_LANE
        if (environment or "").strip().lower() == "ec2"
        else DEFAULT_EXECUTION_LANE
    )


def _context_from_run(run: SandboxRunModel) -> SandboxLaunchContext:
    return SandboxLaunchContext(
        sandbox_run_id=run.id,
        worker_job_id=run.worker_job_id,
        worker_job_attempt=run.worker_job_attempt,
        trial_id=run.trial_id,
        launch_token=run.launch_token,
        deployment=run.deployment,
        aws_account_id=run.aws_account_id,
        region=run.region,
    )


async def create_ec2_sandbox_run(
    *,
    worker_job_id: str,
    worker_job_attempt: int,
    trial_id: str,
) -> SandboxLaunchContext:
    """Create the attempt ledger before Harbor can launch provider state."""
    from oddish.runtime.registry import get_backend

    backend = cast(_Ec2LifecycleBackend | None, get_backend("ec2"))
    if backend is None:
        raise SandboxLifecycleError("EC2 backend is not registered")
    deployment = backend.deployment_name()
    region = str(backend.configured_region()).strip()
    aws_account_id = await backend.resolve_aws_account_id_async()
    if not region:
        raise SandboxLifecycleError("EC2 region is missing")

    async with get_session() as session:
        worker = await session.scalar(
            select(WorkerJobModel)
            .where(WorkerJobModel.id == worker_job_id)
            .with_for_update()
        )
        if worker is None:
            raise SandboxLifecycleError(f"worker_job {worker_job_id} does not exist")
        if worker.status != WorkerJobStatus.RUNNING:
            raise SandboxProvisioningRefused(
                f"worker_job {worker_job_id} is {worker.status.value}, not RUNNING"
            )
        if worker.attempts != worker_job_attempt:
            raise SandboxProvisioningRefused(
                f"worker_job {worker_job_id} attempt changed from "
                f"{worker_job_attempt} to {worker.attempts}"
            )
        if worker.execution_lane != EC2_TRIAL_EXECUTION_LANE:
            raise SandboxLifecycleError(
                f"worker_job {worker_job_id} is not on the EC2 execution lane"
            )
        existing = await session.scalar(
            select(SandboxRunModel).where(
                SandboxRunModel.worker_job_id == worker_job_id,
                SandboxRunModel.worker_job_attempt == worker_job_attempt,
            )
        )
        if existing is not None:
            if existing.trial_id != trial_id or existing.provider != "ec2":
                raise SandboxLifecycleError(
                    f"sandbox run {existing.id} does not match this EC2 trial"
                )
            return _context_from_run(existing)

        run = SandboxRunModel(
            id=generate_id(),
            worker_job_id=worker_job_id,
            worker_job_attempt=worker_job_attempt,
            trial_id=trial_id,
            provider="ec2",
            state=SandboxRunState.PROVISIONING.value,
            deployment=deployment,
            aws_account_id=aws_account_id,
            region=region,
            launch_token=secrets.token_urlsafe(24),
        )
        session.add(run)
        await session.flush()
        return _context_from_run(run)


async def mark_environment_provisioned(
    *,
    context: SandboxLaunchContext,
    provider: str | None,
    external_id: str | None,
    worker_id: str | None,
) -> None:
    """Persist provider identity while serializing against cancellation."""
    if (provider or "").strip().lower() != "ec2":
        raise SandboxLifecycleError(
            f"sandbox run {context.sandbox_run_id} provisioned as {provider!r}"
        )
    match = _EC2_HANDLE.fullmatch(external_id or "")
    if match is None:
        raise SandboxLifecycleError(
            f"sandbox run {context.sandbox_run_id} emitted malformed EC2 handle"
        )
    if (
        match.group("account") != context.aws_account_id
        or match.group("region") != context.region
    ):
        raise SandboxLifecycleError(
            f"sandbox run {context.sandbox_run_id} emitted an ownership-mismatched handle"
        )

    async with get_session() as session:
        worker = await session.scalar(
            select(WorkerJobModel)
            .where(WorkerJobModel.id == context.worker_job_id)
            .with_for_update()
        )
        run = await session.scalar(
            select(SandboxRunModel)
            .where(SandboxRunModel.id == context.sandbox_run_id)
            .with_for_update()
        )
        if worker is None or run is None:
            raise SandboxLifecycleError("sandbox launch owner disappeared")
        worker_matches = (
            worker.status == WorkerJobStatus.RUNNING
            and worker.attempts == context.worker_job_attempt
            and (worker_id is None or worker.current_worker_id == worker_id)
        )
        if not worker_matches or run.state == SandboxRunState.TERMINATING.value:
            if run.external_id is not None and run.external_id != external_id:
                raise SandboxLifecycleError(
                    f"sandbox run {run.id} attempted to replace its external handle"
                )
            # Persist the late handle even though teardown already won. This is
            # what makes the instance recoverable by the finalizer/reconciler.
            run.external_id = external_id
            run.provisioned_at = run.provisioned_at or datetime.now(timezone.utc)
            if run.state not in {
                SandboxRunState.TERMINATING.value,
                SandboxRunState.TERMINATED.value,
            }:
                run.state = SandboxRunState.TERMINATING.value
                run.termination_requested_at = datetime.now(timezone.utc)
            await session.commit()
            raise SandboxProvisioningRefused(
                f"sandbox run {run.id} lost its worker before provisioning persisted"
            )
        if run.state == SandboxRunState.TERMINATED.value:
            raise SandboxProvisioningRefused(
                f"sandbox run {run.id} was already terminated"
            )
        if run.external_id is not None and run.external_id != external_id:
            raise SandboxLifecycleError(
                f"sandbox run {run.id} attempted to replace its external handle"
            )
        run.external_id = external_id
        run.provisioned_at = run.provisioned_at or datetime.now(timezone.utc)
        run.state = SandboxRunState.RUNNING.value
        worker.provider = "ec2"
        worker.external_id = external_id
        worker.sandbox_creating_at = worker.sandbox_creating_at or datetime.now(
            timezone.utc
        )


async def request_sandbox_termination(
    sandbox_run_id: str, *, error: str | None = None
) -> SandboxRunModel | None:
    async with get_session() as session:
        run = await session.scalar(
            select(SandboxRunModel)
            .where(SandboxRunModel.id == sandbox_run_id)
            .with_for_update()
        )
        if run is None:
            return None
        if run.state != SandboxRunState.TERMINATED.value:
            run.state = SandboxRunState.TERMINATING.value
            run.termination_requested_at = run.termination_requested_at or datetime.now(
                timezone.utc
            )
        if error:
            run.last_error = error[:2000]
        await session.flush()
        return cast(SandboxRunModel, run)


async def mark_sandbox_failed(sandbox_run_id: str, *, error: str) -> None:
    async with get_session() as session:
        run = await session.scalar(
            select(SandboxRunModel)
            .where(SandboxRunModel.id == sandbox_run_id)
            .with_for_update()
        )
        if run is None:
            return
        run.last_error = error[:2000]
        if run.state not in {
            SandboxRunState.TERMINATING.value,
            SandboxRunState.TERMINATED.value,
        }:
            run.state = SandboxRunState.FAILED.value


async def mark_sandbox_terminated(sandbox_run_id: str) -> None:
    async with get_session() as session:
        run = await session.scalar(
            select(SandboxRunModel)
            .where(SandboxRunModel.id == sandbox_run_id)
            .with_for_update()
        )
        if run is None:
            return
        run.state = SandboxRunState.TERMINATED.value
        run.termination_requested_at = run.termination_requested_at or datetime.now(
            timezone.utc
        )
        run.terminated_at = run.terminated_at or datetime.now(timezone.utc)
        run.last_error = None


async def get_sandbox_ownership_by_external_id(
    external_id: str,
) -> tuple[str, Ec2SandboxOwnership] | None:
    async with get_session() as session:
        run = await session.scalar(
            select(SandboxRunModel).where(
                SandboxRunModel.provider == "ec2",
                SandboxRunModel.external_id == external_id,
            )
        )
        if run is None:
            return None
        context = _context_from_run(run)
        return run.id, context.ownership(external_id)


async def terminate_sandbox_run(sandbox_run_id: str) -> bool:
    run = await request_sandbox_termination(sandbox_run_id)
    if run is None:
        return False
    if not run.external_id:
        await mark_sandbox_failed(
            run.id,
            error="teardown is waiting for the provider identity to be persisted",
        )
        return False

    from oddish.runtime.registry import get_backend

    backend = cast(_Ec2LifecycleBackend | None, get_backend(run.provider))
    if backend is None:
        await mark_sandbox_failed(
            run.id, error=f"provider backend {run.provider!r} is not registered"
        )
        return False
    ownership = _context_from_run(run).ownership(run.external_id)
    terminated = await backend.teardown_owned(run.external_id, ownership=ownership)
    if terminated:
        await mark_sandbox_terminated(run.id)
        return True
    await mark_sandbox_failed(run.id, error="provider teardown failed or was refused")
    return False


async def terminate_sandbox_by_external_id(external_id: str) -> bool:
    owned = await get_sandbox_ownership_by_external_id(external_id)
    if owned is None:
        logger.error(
            "metric=sandbox_teardown outcome=refused reason=missing_ledger external_id=%s",
            external_id,
        )
        return False
    return await terminate_sandbox_run(owned[0])


__all__ = [
    "DEFAULT_EXECUTION_LANE",
    "EC2_TRIAL_EXECUTION_LANE",
    "SandboxLaunchContext",
    "SandboxLifecycleError",
    "SandboxProvisioningRefused",
    "create_ec2_sandbox_run",
    "execution_lane_for_environment",
    "get_sandbox_ownership_by_external_id",
    "mark_environment_provisioned",
    "mark_sandbox_failed",
    "mark_sandbox_terminated",
    "request_sandbox_termination",
    "terminate_sandbox_by_external_id",
    "terminate_sandbox_run",
]
