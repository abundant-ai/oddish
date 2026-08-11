"""Pure decision policy for reconciling Oddish-managed EC2 instances."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

DEFAULT_EC2_ORPHAN_GRACE = timedelta(minutes=30)
DEFAULT_EC2_HARD_MAX_AGE = timedelta(hours=14)


@dataclass(frozen=True)
class Ec2InstanceSnapshot:
    instance_id: str
    region: str
    state: str
    launch_time: datetime | None
    managed_tag: str | None
    deployment_tag: str | None
    account_id_tag: str | None
    trial_id_tag: str | None
    worker_job_id_tag: str | None
    sandbox_run_id_tag: str | None = None
    worker_attempt_tag: str | None = None
    launch_token_tag: str | None = None

    @property
    def external_id(self) -> str:
        return f"ec2://{self.account_id_tag}/{self.region}/{self.instance_id}"


@dataclass(frozen=True)
class Ec2InventorySnapshot:
    expected_account_id: str
    expected_deployment: str
    instances: tuple[Ec2InstanceSnapshot, ...]


@dataclass(frozen=True)
class Ec2WorkerLiveness:
    worker_job_id: str
    trial_id: str | None
    status: str
    provider: str | None
    external_id: str | None
    heartbeat_fresh: bool


class Ec2OrphanVerdict(StrEnum):
    KEEP = "keep"
    TERMINATE = "terminate"
    REFUSE = "refuse"


class Ec2OrphanReason(StrEnum):
    WRONG_MANAGED_OWNERSHIP = "wrong_managed_ownership"
    WRONG_DEPLOYMENT = "wrong_deployment"
    WRONG_ACCOUNT = "wrong_account"
    ALREADY_TERMINATING = "already_terminating"
    MISSING_LAUNCH_TIME = "missing_launch_time"
    HARD_MAX_AGE = "hard_max_age"
    LINKED_LIVE_WORKER = "linked_live_worker"
    UNLINKED_LIVE_TRIAL = "unlinked_live_trial"
    WITHIN_GRACE = "within_grace"
    MISSING_OWNER = "missing_owner"
    TERMINAL_OWNER = "terminal_owner"
    STALE_OWNER = "stale_owner"
    HANDLE_MISMATCH = "handle_mismatch"
    UNLINKED_WITHOUT_LIVE_TRIAL = "unlinked_without_live_trial"


@dataclass(frozen=True)
class Ec2OrphanDecision:
    verdict: Ec2OrphanVerdict
    reason: Ec2OrphanReason

    @property
    def should_terminate(self) -> bool:
        return self.verdict is Ec2OrphanVerdict.TERMINATE


def _decision(verdict: Ec2OrphanVerdict, reason: Ec2OrphanReason) -> Ec2OrphanDecision:
    return Ec2OrphanDecision(verdict=verdict, reason=reason)


def _require_aware(value: datetime, *, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _is_running(worker: Ec2WorkerLiveness) -> bool:
    return worker.status.strip().upper() == "RUNNING"


def _is_exact_link(worker: Ec2WorkerLiveness, instance: Ec2InstanceSnapshot) -> bool:
    return (
        worker.provider or ""
    ).strip().lower() == "ec2" and worker.external_id == instance.external_id


def _is_unlinked_from_inventory(
    worker: Ec2WorkerLiveness, inventory_handles: frozenset[str]
) -> bool:
    provider = (worker.provider or "").strip().lower()
    external_id = (worker.external_id or "").strip()
    return (
        provider != "ec2"
        or not external_id.lower().startswith("ec2://")
        or external_id not in inventory_handles
    )


def decide_ec2_orphan(
    instance: Ec2InstanceSnapshot,
    workers: tuple[Ec2WorkerLiveness, ...],
    *,
    expected_deployment: str,
    expected_account_id: str,
    now: datetime,
    inventory_handles: frozenset[str],
    grace: timedelta = DEFAULT_EC2_ORPHAN_GRACE,
    hard_max_age: timedelta = DEFAULT_EC2_HARD_MAX_AGE,
) -> Ec2OrphanDecision:
    """Return a deterministic decision from already-snapshotted state."""

    if instance.managed_tag != "true":
        return _decision(
            Ec2OrphanVerdict.REFUSE,
            Ec2OrphanReason.WRONG_MANAGED_OWNERSHIP,
        )
    if instance.deployment_tag != expected_deployment:
        return _decision(
            Ec2OrphanVerdict.REFUSE,
            Ec2OrphanReason.WRONG_DEPLOYMENT,
        )
    if instance.account_id_tag != expected_account_id:
        return _decision(
            Ec2OrphanVerdict.REFUSE,
            Ec2OrphanReason.WRONG_ACCOUNT,
        )

    if instance.state.strip().lower() in {"shutting-down", "terminated"}:
        return _decision(
            Ec2OrphanVerdict.KEEP,
            Ec2OrphanReason.ALREADY_TERMINATING,
        )

    if instance.launch_time is None:
        return _decision(
            Ec2OrphanVerdict.KEEP,
            Ec2OrphanReason.MISSING_LAUNCH_TIME,
        )

    _require_aware(now, name="now")
    _require_aware(instance.launch_time, name="instance.launch_time")
    age = now - instance.launch_time
    if age >= hard_max_age:
        return _decision(
            Ec2OrphanVerdict.TERMINATE,
            Ec2OrphanReason.HARD_MAX_AGE,
        )

    linked_live_worker = next(
        (
            worker
            for worker in workers
            if _is_running(worker)
            and worker.heartbeat_fresh
            and _is_exact_link(worker, instance)
        ),
        None,
    )
    if linked_live_worker is not None:
        return _decision(
            Ec2OrphanVerdict.KEEP,
            Ec2OrphanReason.LINKED_LIVE_WORKER,
        )

    tagged_owner = next(
        (
            worker
            for worker in workers
            if worker.worker_job_id == instance.worker_job_id_tag
        ),
        None,
    )
    has_unlinked_live_trial = bool(instance.trial_id_tag) and any(
        _is_running(worker)
        and worker.heartbeat_fresh
        and worker.trial_id == instance.trial_id_tag
        and _is_unlinked_from_inventory(worker, inventory_handles)
        for worker in workers
    )
    if has_unlinked_live_trial:
        return _decision(
            Ec2OrphanVerdict.KEEP,
            Ec2OrphanReason.UNLINKED_LIVE_TRIAL,
        )

    if age <= grace:
        return _decision(
            Ec2OrphanVerdict.KEEP,
            Ec2OrphanReason.WITHIN_GRACE,
        )

    if tagged_owner is None:
        return _decision(
            Ec2OrphanVerdict.TERMINATE,
            Ec2OrphanReason.MISSING_OWNER,
        )
    if not _is_running(tagged_owner):
        return _decision(
            Ec2OrphanVerdict.TERMINATE,
            Ec2OrphanReason.TERMINAL_OWNER,
        )
    if not tagged_owner.heartbeat_fresh:
        return _decision(
            Ec2OrphanVerdict.TERMINATE,
            Ec2OrphanReason.STALE_OWNER,
        )
    if tagged_owner.external_id:
        return _decision(
            Ec2OrphanVerdict.TERMINATE,
            Ec2OrphanReason.HANDLE_MISMATCH,
        )
    return _decision(
        Ec2OrphanVerdict.TERMINATE,
        Ec2OrphanReason.UNLINKED_WITHOUT_LIVE_TRIAL,
    )


__all__ = [
    "DEFAULT_EC2_HARD_MAX_AGE",
    "DEFAULT_EC2_ORPHAN_GRACE",
    "Ec2InstanceSnapshot",
    "Ec2InventorySnapshot",
    "Ec2OrphanDecision",
    "Ec2OrphanReason",
    "Ec2OrphanVerdict",
    "Ec2WorkerLiveness",
    "decide_ec2_orphan",
]
