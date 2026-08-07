from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from oddish.runtime.ec2_orphans import (
    Ec2InstanceSnapshot,
    Ec2OrphanDecision,
    Ec2OrphanReason,
    Ec2OrphanVerdict,
    Ec2WorkerLiveness,
    decide_ec2_orphan,
)

NOW = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
ACCOUNT = "123456789012"
DEPLOYMENT = "oddish-prod"
INSTANCE_ID = "i-123"
REGION = "us-east-1"
HANDLE = f"ec2://{ACCOUNT}/{REGION}/{INSTANCE_ID}"


def _instance(**overrides: object) -> Ec2InstanceSnapshot:
    values = {
        "instance_id": INSTANCE_ID,
        "region": REGION,
        "state": "running",
        "launch_time": NOW - timedelta(hours=1),
        "managed_tag": "true",
        "deployment_tag": DEPLOYMENT,
        "account_id_tag": ACCOUNT,
        "trial_id_tag": "trial-1",
        "worker_job_id_tag": "job-1",
    }
    values.update(overrides)
    return Ec2InstanceSnapshot(**values)


def _worker(**overrides: object) -> Ec2WorkerLiveness:
    values = {
        "worker_job_id": "job-1",
        "trial_id": "trial-1",
        "status": "RUNNING",
        "provider": "ec2",
        "external_id": HANDLE,
        "heartbeat_fresh": True,
    }
    values.update(overrides)
    return Ec2WorkerLiveness(**values)


def _decide(
    instance: Ec2InstanceSnapshot,
    *workers: Ec2WorkerLiveness,
    inventory_handles: frozenset[str] | None = None,
):
    return decide_ec2_orphan(
        instance,
        tuple(workers),
        expected_deployment=DEPLOYMENT,
        expected_account_id=ACCOUNT,
        now=NOW,
        inventory_handles=(
            inventory_handles
            if inventory_handles is not None
            else frozenset({instance.external_id})
        ),
    )


def test_snapshots_and_decisions_are_immutable() -> None:
    instance = _instance()
    worker = _worker()
    decision = _decide(instance, worker)

    with pytest.raises(FrozenInstanceError):
        instance.state = "terminated"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        worker.status = "FAILED"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        decision.reason = Ec2OrphanReason.HARD_MAX_AGE  # type: ignore[misc]


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"managed_tag": None}, Ec2OrphanReason.WRONG_MANAGED_OWNERSHIP),
        ({"managed_tag": "false"}, Ec2OrphanReason.WRONG_MANAGED_OWNERSHIP),
        ({"deployment_tag": "other"}, Ec2OrphanReason.WRONG_DEPLOYMENT),
        ({"account_id_tag": "999999999999"}, Ec2OrphanReason.WRONG_ACCOUNT),
    ],
)
def test_wrong_ownership_is_always_refused_even_past_hard_max(
    override: dict[str, object], reason: Ec2OrphanReason
) -> None:
    instance = _instance(
        launch_time=NOW - timedelta(days=2),
        **override,
    )

    decision = _decide(instance)

    assert decision == Ec2OrphanDecision(
        verdict=Ec2OrphanVerdict.REFUSE,
        reason=reason,
    )


@pytest.mark.parametrize("state", ["shutting-down", "terminated"])
def test_already_terminating_instances_are_kept_without_a_target(state: str) -> None:
    decision = _decide(_instance(state=state, launch_time=NOW - timedelta(days=2)))

    assert decision.verdict is Ec2OrphanVerdict.KEEP
    assert decision.reason is Ec2OrphanReason.ALREADY_TERMINATING


def test_missing_launch_time_is_kept_conservatively() -> None:
    decision = _decide(_instance(launch_time=None))

    assert decision.verdict is Ec2OrphanVerdict.KEEP
    assert decision.reason is Ec2OrphanReason.MISSING_LAUNCH_TIME


def test_hard_max_age_at_or_over_fourteen_hours_deletes_even_live() -> None:
    older = _decide(
        _instance(launch_time=NOW - timedelta(hours=14, microseconds=1)),
        _worker(),
    )
    boundary = _decide(
        _instance(launch_time=NOW - timedelta(hours=14)),
        _worker(),
    )

    assert older.verdict is Ec2OrphanVerdict.TERMINATE
    assert older.reason is Ec2OrphanReason.HARD_MAX_AGE
    assert boundary.verdict is Ec2OrphanVerdict.TERMINATE
    assert boundary.reason is Ec2OrphanReason.HARD_MAX_AGE


def test_linked_fresh_running_worker_with_exact_ec2_handle_keeps() -> None:
    decision = _decide(_instance(), _worker())

    assert decision.verdict is Ec2OrphanVerdict.KEEP
    assert decision.reason is Ec2OrphanReason.LINKED_LIVE_WORKER


@pytest.mark.parametrize(
    "tag_overrides",
    [
        {"worker_job_id_tag": None, "trial_id_tag": None},
        {"worker_job_id_tag": "stale-job", "trial_id_tag": "stale-trial"},
    ],
)
def test_exact_live_handle_keeps_when_owner_tags_are_missing_or_stale(
    tag_overrides: dict[str, object],
) -> None:
    instance = _instance(**tag_overrides)
    worker = _worker(worker_job_id="current-job", trial_id="current-trial")

    decision = _decide(instance, worker)

    assert decision.verdict is Ec2OrphanVerdict.KEEP
    assert decision.reason is Ec2OrphanReason.LINKED_LIVE_WORKER


def test_unlinked_instance_keeps_only_for_same_trial_fresh_running_worker() -> None:
    matching = _decide(_instance(), _worker(external_id=None))
    other_trial = _decide(
        _instance(),
        _worker(worker_job_id="job-2", trial_id="trial-2", external_id=None),
    )
    linked_elsewhere = _decide(
        _instance(),
        _worker(worker_job_id="job-2", external_id="ec2://other"),
    )

    assert matching.reason is Ec2OrphanReason.UNLINKED_LIVE_TRIAL
    assert matching.verdict is Ec2OrphanVerdict.KEEP
    assert other_trial.verdict is Ec2OrphanVerdict.TERMINATE
    assert linked_elsewhere.reason is Ec2OrphanReason.UNLINKED_LIVE_TRIAL
    assert linked_elsewhere.verdict is Ec2OrphanVerdict.KEEP


def test_same_trial_guard_does_not_keep_old_instance_linked_to_current_instance() -> None:
    old_instance = _instance(launch_time=NOW - timedelta(hours=1))
    current_handle = f"ec2://{ACCOUNT}/{REGION}/i-current"
    current_worker = _worker(external_id=current_handle)

    decision = _decide(
        old_instance,
        current_worker,
        inventory_handles=frozenset({old_instance.external_id, current_handle}),
    )

    assert decision.verdict is Ec2OrphanVerdict.TERMINATE
    assert decision.reason is Ec2OrphanReason.HANDLE_MISMATCH


def test_same_trial_guard_keeps_new_instance_when_old_worker_handle_is_absent() -> None:
    new_instance = _instance(launch_time=NOW - timedelta(hours=1))
    absent_old_handle = f"ec2://{ACCOUNT}/{REGION}/i-absent-old"
    stale_worker_link = _worker(external_id=absent_old_handle)

    decision = _decide(
        new_instance,
        stale_worker_link,
        inventory_handles=frozenset({new_instance.external_id}),
    )

    assert decision.verdict is Ec2OrphanVerdict.KEEP
    assert decision.reason is Ec2OrphanReason.UNLINKED_LIVE_TRIAL


@pytest.mark.parametrize(
    "worker",
    [
        None,
        _worker(status="FAILED", heartbeat_fresh=False),
        _worker(heartbeat_fresh=False),
        _worker(
            trial_id="different-trial",
            provider="ec2",
            external_id="ec2://wrong",
        ),
    ],
)
def test_grace_keeps_before_missing_terminal_stale_or_mismatched_judgment(
    worker: Ec2WorkerLiveness | None,
) -> None:
    instance = _instance(launch_time=NOW - timedelta(minutes=29, seconds=59))

    decision = _decide(instance, *((worker,) if worker else ()))

    assert decision.verdict is Ec2OrphanVerdict.KEEP
    assert decision.reason is Ec2OrphanReason.WITHIN_GRACE


@pytest.mark.parametrize(
    ("workers", "reason"),
    [
        ((), Ec2OrphanReason.MISSING_OWNER),
        ((_worker(status="FAILED"),), Ec2OrphanReason.TERMINAL_OWNER),
        ((_worker(heartbeat_fresh=False),), Ec2OrphanReason.STALE_OWNER),
        (
            (
                _worker(
                    trial_id="different-trial",
                    provider="ec2",
                    external_id="ec2://wrong",
                ),
            ),
            Ec2OrphanReason.HANDLE_MISMATCH,
        ),
        (
            (_worker(external_id=None, heartbeat_fresh=False),),
            Ec2OrphanReason.STALE_OWNER,
        ),
    ],
)
def test_past_grace_missing_terminal_stale_and_mismatched_instances_delete(
    workers: tuple[Ec2WorkerLiveness, ...], reason: Ec2OrphanReason
) -> None:
    decision = _decide(
        _instance(launch_time=NOW - timedelta(minutes=30, microseconds=1)),
        *workers,
    )

    assert decision.verdict is Ec2OrphanVerdict.TERMINATE
    assert decision.reason is reason


def test_decision_rejects_naive_time_inputs() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        decide_ec2_orphan(
            _instance(),
            (),
            expected_deployment=DEPLOYMENT,
            expected_account_id=ACCOUNT,
            now=NOW.replace(tzinfo=None),
            inventory_handles=frozenset({HANDLE}),
        )
