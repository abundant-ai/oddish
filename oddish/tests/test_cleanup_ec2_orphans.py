from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from oddish.runtime.ec2_orphans import Ec2InstanceSnapshot, Ec2InventorySnapshot
from oddish.workers.queue import cleanup


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
ACCOUNT_ID = "123456789012"
DEPLOYMENT = "oddish-test"


class _MappingsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _LivenessSession:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        ledger_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.rows = rows
        self.ledger_rows = ledger_rows or []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, statement, params: dict[str, Any]):
        rendered = str(statement)
        self.execute_calls.append((rendered, params))
        return _MappingsResult(
            self.ledger_rows if "FROM sandbox_runs" in rendered else self.rows
        )

    async def scalar(self, _statement):
        return NOW


def _snapshot(
    *,
    age: timedelta,
    worker_job_id: str | None = "job-1",
    trial_id: str | None = "trial-1",
    deployment: str = DEPLOYMENT,
) -> Ec2InstanceSnapshot:
    return Ec2InstanceSnapshot(
        instance_id="i-0123456789abcdef0",
        region="us-east-1",
        state="running",
        launch_time=NOW - age,
        managed_tag="true",
        deployment_tag=deployment,
        account_id_tag=ACCOUNT_ID,
        trial_id_tag=trial_id,
        worker_job_id_tag=worker_job_id,
        sandbox_run_id_tag="sandbox-run-1",
        worker_attempt_tag="1",
        launch_token_tag="launch-token-1",
    )


def _ledger(snapshot: Ec2InstanceSnapshot, *, state: str = "RUNNING") -> dict[str, Any]:
    return {
        "id": snapshot.sandbox_run_id_tag,
        "worker_job_id": snapshot.worker_job_id_tag,
        "worker_job_attempt": int(snapshot.worker_attempt_tag or "0"),
        "trial_id": snapshot.trial_id_tag,
        "provider": "ec2",
        "state": state,
        "deployment": snapshot.deployment_tag,
        "aws_account_id": snapshot.account_id_tag,
        "region": snapshot.region,
        "launch_token": snapshot.launch_token_tag,
        "external_id": snapshot.external_id,
    }


def _worker(
    *,
    worker_job_id: str = "job-1",
    trial_id: str = "trial-1",
    status: str = "RUNNING",
    provider: str | None = "ec2",
    external_id: str | None = None,
    heartbeat_fresh: bool = True,
) -> dict[str, Any]:
    return {
        "id": worker_job_id,
        "subject_id": trial_id,
        "status": status,
        "provider": provider,
        "external_id": external_id,
        "heartbeat_fresh": heartbeat_fresh,
    }


@pytest.mark.parametrize(
    ("snapshot", "workers", "expected_target", "expected_reason"),
    [
        (
            _snapshot(age=timedelta(hours=1)),
            [_worker(external_id=f"ec2://{ACCOUNT_ID}/us-east-1/i-0123456789abcdef0")],
            False,
            "linked_live_worker",
        ),
        (
            _snapshot(age=timedelta(minutes=31)),
            [
                _worker(
                    external_id=(f"ec2://{ACCOUNT_ID}/us-east-1/i-0123456789abcdef0"),
                    heartbeat_fresh=False,
                )
            ],
            True,
            "stale_owner",
        ),
    ],
)
@pytest.mark.asyncio
async def test_ec2_orphan_liveness_batch_drives_expected_verdicts(
    monkeypatch: pytest.MonkeyPatch,
    snapshot: Ec2InstanceSnapshot,
    workers: list[dict[str, Any]],
    expected_target: bool,
    expected_reason: str,
) -> None:
    session = _LivenessSession(workers, [_ledger(snapshot)])
    lines: list[str] = []
    monkeypatch.setattr(cleanup.console, "print", lines.append)

    targets, kept = await cleanup._decide_ec2_orphan_targets(
        session,
        (snapshot,),
        expected_deployment=DEPLOYMENT,
        expected_account_id=ACCOUNT_ID,
        stale_after_minutes=15,
    )

    expected_handle = (
        "ec2",
        f"ec2://{ACCOUNT_ID}/us-east-1/i-0123456789abcdef0",
    )
    assert (expected_handle in targets) is expected_target
    assert kept == (0 if expected_target else 1)
    assert len(session.execute_calls) == 2
    statement, params = session.execute_calls[0]
    assert "deleted_at IS NULL" in statement
    assert "kind = 'TRIAL'" in statement
    assert "subject_table = 'trials'" in statement
    assert "heartbeat_at >= NOW() - make_interval" in statement
    assert params == {
        "worker_job_ids": (
            [snapshot.worker_job_id_tag] if snapshot.worker_job_id_tag else []
        ),
        "trial_ids": [snapshot.trial_id_tag] if snapshot.trial_id_tag else [],
        "external_ids": [snapshot.external_id],
        "stale_after_minutes": 15,
    }
    ledger_statement, ledger_params = session.execute_calls[1]
    assert "FROM sandbox_runs" in ledger_statement
    assert ledger_params == {"sandbox_run_ids": ["sandbox-run-1"]}
    assert any(
        "metric=ec2_orphan_verdict" in line and f"reason={expected_reason}" in line
        for line in lines
    )


@pytest.mark.asyncio
async def test_ec2_orphan_refuses_destructive_action_without_exact_ledger_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(age=timedelta(hours=15))
    snapshot = Ec2InstanceSnapshot(
        **{
            **snapshot.__dict__,
            "launch_token_tag": "wrong-token",
        }
    )
    session = _LivenessSession([], [_ledger(snapshot) | {"launch_token": "real-token"}])
    lines: list[str] = []
    monkeypatch.setattr(cleanup.console, "print", lines.append)

    targets, kept = await cleanup._decide_ec2_orphan_targets(
        session,
        (snapshot,),
        expected_deployment=DEPLOYMENT,
        expected_account_id=ACCOUNT_ID,
        stale_after_minutes=15,
    )

    assert targets == set()
    assert kept == 0
    assert any("reason=ledger_ownership_mismatch" in line for line in lines)


def _patch_unrelated_cleanup_phases(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[str],
    session: object,
    termination_result: int = 0,
    stale_targets: set[tuple[str, str]] | None = None,
) -> list[set[tuple[str, str]]]:
    @asynccontextmanager
    async def fake_get_session():
        events.append("db_open")
        yield session
        events.append("db_commit")

    async def zero(*_args, **_kwargs):
        return 0

    async def reap(*_args, **_kwargs):
        events.append("stale_reap")
        return 0, 0, [], set(stale_targets or ())

    async def unwedge(*_args, **_kwargs):
        return 0, 0, 0

    terminated_targets: list[set[tuple[str, str]]] = []

    async def terminate(targets: set[tuple[str, str]]) -> int:
        events.append("terminate")
        terminated_targets.append(set(targets))
        return termination_result

    monkeypatch.setattr(cleanup, "get_session", fake_get_session)
    monkeypatch.setattr(cleanup, "reap_idle_in_transaction_zombies", zero)
    monkeypatch.setattr(cleanup, "cleanup_sandbox_capacity_leases", zero)
    monkeypatch.setattr(cleanup, "_reap_stale_worker_jobs", reap)
    monkeypatch.setattr(cleanup, "_advance_running_tasks_to_analysis", zero)
    monkeypatch.setattr(cleanup, "_advance_legacy_analyzing_tasks", zero)
    monkeypatch.setattr(cleanup, "_heal_stale_verdict_pending", zero)
    monkeypatch.setattr(cleanup, "_unwedge_stuck_analyzing", unwedge)
    monkeypatch.setattr(cleanup, "_release_orphaned_slots", zero)
    monkeypatch.setattr(cleanup, "_reconcile_experiment_last_activity", zero)
    monkeypatch.setattr(cleanup, "_maybe_reconcile_tag_projections", zero)
    monkeypatch.setattr(cleanup, "sweep_orphaned_tag_owners", zero)
    monkeypatch.setattr(cleanup, "_terminate_orphaned_sandboxes", terminate)
    monkeypatch.setattr(cleanup, "clear_terminal_trial_runtime_refs", zero)
    monkeypatch.setattr(cleanup, "purge_stale_trial_events", zero)
    monkeypatch.setattr(cleanup, "reconcile_compute_cost_spans", zero)
    return terminated_targets


@pytest.mark.asyncio
async def test_ec2_snapshot_failure_does_not_block_database_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Backend:
        async def snapshot_managed_instances(self):
            events.append("snapshot")
            raise RuntimeError("AWS unavailable")

    lines: list[str] = []
    session = _LivenessSession([])
    terminated_targets = _patch_unrelated_cleanup_phases(
        monkeypatch, events=events, session=session
    )
    monkeypatch.setattr(cleanup.settings, "ec2_enabled", True)
    monkeypatch.setattr(cleanup, "get_backend", lambda _name: Backend())
    monkeypatch.setattr(cleanup.console, "print", lines.append)

    result = await cleanup.cleanup_orphaned_queue_state()

    assert events.index("snapshot") < events.index("db_open")
    assert "stale_reap" in events
    assert terminated_targets == [set()]
    assert result["ec2_orphan_snapshot_errors"] == 1
    assert any("metric=ec2_orphan_snapshot_error" in line for line in lines)


@pytest.mark.asyncio
async def test_ec2_orphan_target_is_terminated_only_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    instance = _snapshot(age=timedelta(minutes=31))
    expected = {("ec2", instance.external_id)}

    class Backend:
        async def snapshot_managed_instances(self):
            events.append("snapshot")
            return Ec2InventorySnapshot(
                expected_account_id=ACCOUNT_ID,
                expected_deployment=DEPLOYMENT,
                instances=(instance,),
            )

        def resolve_aws_account_id(self) -> str:
            raise AssertionError("cleanup must reuse the inventory account")

        def deployment_name(self) -> str:
            raise AssertionError("cleanup must reuse the inventory deployment")

    session = _LivenessSession([], [_ledger(instance)])
    terminated_targets = _patch_unrelated_cleanup_phases(
        monkeypatch,
        events=events,
        session=session,
        termination_result=0,
        stale_targets=expected,
    )
    monkeypatch.setattr(cleanup.settings, "ec2_enabled", True)
    monkeypatch.setattr(cleanup, "get_backend", lambda _name: Backend())

    result = await cleanup.cleanup_orphaned_queue_state()

    # Stale reap and the inventory decision identified the same instance. The
    # shared set must send exactly one post-commit target to teardown.
    assert terminated_targets == [expected]
    assert events.index("snapshot") < events.index("db_open")
    assert events.index("db_commit") < events.index("terminate")
    assert result["ec2_orphan_terminate_candidates"] == 1
    assert result["worker_sandboxes_terminated"] == 0


@pytest.mark.asyncio
async def test_ec2_teardown_error_is_logged_without_aborting_other_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines: list[str] = []

    async def fail_teardown(_provider: str, _external_id: str) -> bool:
        raise RuntimeError("AWS terminate failed")

    monkeypatch.setattr(cleanup, "cancel_job_by_worker", fail_teardown)
    monkeypatch.setattr(cleanup.console, "print", lines.append)

    terminated = await cleanup._terminate_orphaned_sandboxes(
        {("ec2", f"ec2://{ACCOUNT_ID}/us-east-1/i-0123456789abcdef0")}
    )

    assert terminated == 0
    assert any(
        "metric=orphaned_sandbox_termination outcome=error" in line
        and "error_type=RuntimeError" in line
        for line in lines
    )


@pytest.mark.asyncio
async def test_stale_reap_discovers_one_ec2_teardown_target(monkeypatch) -> None:
    handle = f"ec2://{ACCOUNT_ID}/us-east-1/i-stale"

    class Result:
        def __init__(self, *, rows=(), mapping=None):
            self.rows = list(rows)
            self.mapping = mapping

        def all(self):
            return self.rows

        def mappings(self):
            return self

        def one_or_none(self):
            return self.mapping

    class Savepoint:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    class Session:
        def begin_nested(self):
            return Savepoint()

        async def execute(self, statement, _params=None):
            sql = str(statement)
            if sql.lstrip().startswith("SELECT id FROM worker_jobs"):
                return Result(rows=[("job-stale",)])
            if "SET    status = CASE" in sql:
                return Result(
                    mapping={
                        "id": "job-stale",
                        "kind": "TRIAL",
                        "new_status": "FAILED",
                        "subject_table": "trials",
                        "subject_id": "trial-stale",
                        "attempts": 1,
                        "max_attempts": 1,
                        "error_message": "stale",
                        "provider": "ec2",
                        "external_id": handle,
                    }
                )
            return Result()

        async def flush(self):
            return None

    async def mirror(_session, _row):
        return "trial-stale"

    teardown_calls: list[tuple[str, str]] = []

    async def terminate(provider: str, external_id: str) -> bool:
        teardown_calls.append((provider, external_id))
        return True

    monkeypatch.setattr(cleanup, "_mirror_stale_job_to_domain_row", mirror)
    monkeypatch.setattr(cleanup, "cancel_job_by_worker", terminate)

    retried, failed, trial_ids, targets = await cleanup._reap_stale_worker_jobs(
        Session(), stale_after_minutes=15
    )
    terminated = await cleanup._terminate_orphaned_sandboxes(targets)

    assert (retried, failed, trial_ids) == (0, 1, ["trial-stale"])
    assert targets == {("ec2", handle)}
    assert terminated == 1
    assert teardown_calls == [("ec2", handle)]
