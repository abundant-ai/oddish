from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.db import SandboxRunState, WorkerJobStatus
from oddish.runtime import sandbox_lifecycle
from oddish.workers.queue import worker_job_single_job
from oddish.workers.queue import sandbox_capacity
from oddish.workers.queue.worker_job_single_job import claim_single_worker_job


class _Session:
    def __init__(self, scalar_results: list[object | None]) -> None:
        self.scalar_results = list(scalar_results)
        self.added: list[object] = []
        self.commit_count = 0

    async def scalar(self, _statement):
        return self.scalar_results.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.commit_count += 1


def _install_session(monkeypatch: pytest.MonkeyPatch, session: _Session) -> None:
    @asynccontextmanager
    async def get_session():
        yield session

    monkeypatch.setattr(sandbox_lifecycle, "get_session", get_session)


def _context() -> sandbox_lifecycle.SandboxLaunchContext:
    return sandbox_lifecycle.SandboxLaunchContext(
        sandbox_run_id="sandbox-run-1",
        worker_job_id="worker-job-1",
        worker_job_attempt=1,
        trial_id="trial-1",
        launch_token="launch-token-1",
        deployment="oddish-test",
        aws_account_id="123456789012",
        region="us-west-2",
    )


def _run(*, state: str = SandboxRunState.PROVISIONING.value, external_id=None):
    context = _context()
    return SimpleNamespace(
        id=context.sandbox_run_id,
        worker_job_id=context.worker_job_id,
        worker_job_attempt=context.worker_job_attempt,
        trial_id=context.trial_id,
        provider="ec2",
        state=state,
        deployment=context.deployment,
        aws_account_id=context.aws_account_id,
        region=context.region,
        launch_token=context.launch_token,
        external_id=external_id,
        provisioned_at=None,
        termination_requested_at=None,
        terminated_at=None,
        last_error=None,
    )


@pytest.mark.asyncio
async def test_sandbox_run_is_created_before_provider_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = SimpleNamespace(
        id="worker-job-1",
        status=WorkerJobStatus.RUNNING,
        attempts=1,
        execution_lane=sandbox_lifecycle.EC2_TRIAL_EXECUTION_LANE,
    )
    session = _Session([worker, None])
    _install_session(monkeypatch, session)

    class Backend:
        @staticmethod
        def deployment_name() -> str:
            return "oddish-test"

        @staticmethod
        def configured_region() -> str:
            return "us-west-2"

        @staticmethod
        async def resolve_aws_account_id_async() -> str:
            return "123456789012"

    import oddish.runtime.registry as registry

    monkeypatch.setattr(registry, "get_backend", lambda _provider: Backend())

    context = await sandbox_lifecycle.create_ec2_sandbox_run(
        worker_job_id="worker-job-1",
        worker_job_attempt=1,
        trial_id="trial-1",
    )

    assert context.worker_job_id == "worker-job-1"
    assert len(session.added) == 1
    ledger = session.added[0]
    assert ledger.state == SandboxRunState.PROVISIONING.value
    assert ledger.external_id is None
    assert ledger.launch_token


@pytest.mark.asyncio
async def test_late_provisioned_event_preserves_cancellation_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = SimpleNamespace(
        status=WorkerJobStatus.CANCELLED,
        attempts=1,
        current_worker_id="worker-1",
    )
    run = _run()
    session = _Session([worker, run])
    _install_session(monkeypatch, session)
    handle = "ec2://123456789012/us-west-2/i-late"

    with pytest.raises(sandbox_lifecycle.SandboxProvisioningRefused):
        await sandbox_lifecycle.mark_environment_provisioned(
            context=_context(),
            provider="ec2",
            external_id=handle,
            worker_id="worker-1",
        )

    assert run.external_id == handle
    assert run.state == SandboxRunState.TERMINATING.value
    assert run.termination_requested_at is not None
    assert session.commit_count == 1


@pytest.mark.asyncio
async def test_generic_failure_cannot_overwrite_terminating(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(state=SandboxRunState.TERMINATING.value)
    session = _Session([run])
    _install_session(monkeypatch, session)

    await sandbox_lifecycle.mark_sandbox_failed(run.id, error="launch failed")

    assert run.state == SandboxRunState.TERMINATING.value
    assert run.last_error == "launch failed"


@pytest.mark.asyncio
async def test_successful_teardown_is_persisted_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = "ec2://123456789012/us-west-2/i-owned"
    run = _run(state=SandboxRunState.TERMINATING.value, external_id=handle)
    marked: list[str] = []

    async def request(_sandbox_run_id: str):
        return run

    async def mark(sandbox_run_id: str):
        marked.append(sandbox_run_id)

    class Backend:
        async def teardown_owned(self, external_id: str, *, ownership) -> bool:
            assert external_id == handle
            assert ownership.sandbox_run_id == run.id
            return True

    import oddish.runtime.registry as registry

    monkeypatch.setattr(sandbox_lifecycle, "request_sandbox_termination", request)
    monkeypatch.setattr(sandbox_lifecycle, "mark_sandbox_terminated", mark)
    monkeypatch.setattr(registry, "get_backend", lambda _provider: Backend())

    assert await sandbox_lifecycle.terminate_sandbox_run(run.id) is True
    assert marked == [run.id]


@pytest.mark.asyncio
async def test_teardown_without_provider_identity_stays_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(state=SandboxRunState.TERMINATING.value, external_id=None)
    errors: list[str] = []

    async def request(_sandbox_run_id: str):
        return run

    async def failed(_sandbox_run_id: str, *, error: str):
        errors.append(error)

    monkeypatch.setattr(sandbox_lifecycle, "request_sandbox_termination", request)
    monkeypatch.setattr(sandbox_lifecycle, "mark_sandbox_failed", failed)

    assert await sandbox_lifecycle.terminate_sandbox_run(run.id) is False
    assert errors == ["teardown is waiting for the provider identity to be persisted"]


@pytest.mark.asyncio
async def test_ec2_claim_refuses_to_bypass_global_capacity_lease() -> None:
    with pytest.raises(RuntimeError, match="pre-acquired EC2 capacity lease"):
        await claim_single_worker_job(
            "model-queue",
            worker_id="worker-1",
            queue_slot=0,
            execution_lane=sandbox_lifecycle.EC2_TRIAL_EXECUTION_LANE,
        )


@pytest.mark.asyncio
async def test_ec2_claim_renews_capacity_lease_when_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        def __init__(self) -> None:
            self.statement = ""
            self.args = ()

        async def fetchrow(self, statement: str, *args):
            self.statement = statement
            self.args = args
            return None

        async def close(self) -> None:
            return None

    connection = Connection()
    monkeypatch.setattr(
        worker_job_single_job, "_open_connection", lambda: _return(connection)
    )

    claimed = await claim_single_worker_job(
        "model-queue",
        worker_id="worker-1",
        queue_slot=0,
        execution_lane=sandbox_lifecycle.EC2_TRIAL_EXECUTION_LANE,
        capacity_provider="ec2",
        capacity_slot=1,
    )

    assert claimed is None
    assert "locked_until = NOW() + make_interval(secs => $9)" in connection.statement
    assert connection.args[8] == sandbox_capacity.SANDBOX_CAPACITY_LEASE_SECONDS


@pytest.mark.asyncio
async def test_worker_heartbeat_renews_bound_capacity_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        async def execute(self, statement: str, *_args):
            self.statements.append(statement)
            return "UPDATE 1"

        async def fetchrow(self, statement: str, *_args):
            self.statements.append(statement)
            return {"execution_lane": "ec2_trial", "capacity_renewed": True}

        async def close(self) -> None:
            return None

    connection = Connection()
    monkeypatch.setattr(
        worker_job_single_job, "_open_connection", lambda: _return(connection)
    )

    await worker_job_single_job.heartbeat_worker_job(
        "worker-job-1", current_worker_id="worker-1"
    )

    assert any("UPDATE sandbox_capacity_leases" in sql for sql in connection.statements)
    assert any("locked_until = NOW()" in sql for sql in connection.statements)


@pytest.mark.asyncio
async def test_ec2_heartbeat_fails_loudly_when_capacity_lease_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        async def execute(self, _statement: str, *_args):
            return "UPDATE 1"

        async def fetchrow(self, _statement: str, *_args):
            return {"execution_lane": "ec2_trial", "capacity_renewed": False}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        worker_job_single_job, "_open_connection", lambda: _return(Connection())
    )

    with pytest.raises(RuntimeError, match="lost its global capacity lease"):
        await worker_job_single_job.heartbeat_worker_job(
            "worker-job-1", current_worker_id="worker-1"
        )


async def _return(value):
    return value


@pytest.mark.asyncio
async def test_expired_capacity_lease_is_not_stolen_from_running_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Connection:
        def __init__(self) -> None:
            self.available_statement = ""

        @asynccontextmanager
        async def transaction(self):
            yield

        async def execute(self, _statement: str, *_args):
            return "INSERT 0 1"

        async def fetchrow(self, statement: str, *_args):
            self.available_statement = statement
            return None

    connection = Connection()

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield connection

    monkeypatch.setattr(sandbox_capacity, "get_pool", lambda: _return(Pool()))

    acquired = await sandbox_capacity.acquire_sandbox_capacity_lease(
        provider="ec2",
        limit=2,
        worker_id="worker-2",
        lease_seconds=120,
    )

    assert acquired is None
    assert "locked_until <= NOW()" in connection.available_statement
    assert "NOT EXISTS" in connection.available_statement
    assert "wj.status::text = 'RUNNING'" in connection.available_statement
    assert "wj.current_worker_id" in connection.available_statement
    assert "FROM sandbox_runs AS run" in connection.available_statement
    assert "run.terminated_at IS NULL" in connection.available_statement


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"), [("UPDATE 0", False), ("UPDATE 1", True)]
)
async def test_capacity_release_refuses_live_sandbox_runs(
    monkeypatch: pytest.MonkeyPatch, command: str, expected: bool
) -> None:
    class Pool:
        def __init__(self) -> None:
            self.statement = ""

        async def execute(self, statement: str, *_args):
            self.statement = statement
            return command

    pool = Pool()
    monkeypatch.setattr(sandbox_capacity, "get_pool", lambda: _return(pool))

    released = await sandbox_capacity.release_sandbox_capacity_lease(
        provider="ec2", slot=1, worker_id="worker-1"
    )

    assert released is expected
    assert "FROM sandbox_runs AS run" in pool.statement
    assert "run.terminated_at IS NULL" in pool.statement


@pytest.mark.asyncio
async def test_capacity_reconciliation_releases_expired_or_orphaned_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pool:
        def __init__(self) -> None:
            self.statement = ""
            self.args = ()

        async def execute(self, statement: str, *args):
            self.statement = statement
            self.args = args
            return "UPDATE 2"

    pool = Pool()
    monkeypatch.setattr(sandbox_capacity, "get_pool", lambda: _return(pool))

    cleared = await sandbox_capacity.cleanup_sandbox_capacity_leases(grace_seconds=90)

    assert cleared == 2
    assert "lease.locked_until <= NOW()" in pool.statement
    assert "AND NOT EXISTS" in pool.statement
    assert "wj.status::text = 'RUNNING'" in pool.statement
    assert "wj.current_worker_id = lease.locked_by" in pool.statement
    assert "FROM sandbox_runs AS run" in pool.statement
    assert "run.terminated_at IS NULL" in pool.statement
    assert pool.args == (90,)


@pytest.mark.asyncio
async def test_count_held_capacity_counts_every_live_provider_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pool:
        def __init__(self) -> None:
            self.statement = ""
            self.args = ()

        async def fetchval(self, statement: str, *args):
            self.statement = statement
            self.args = args
            return 2

    pool = Pool()
    monkeypatch.setattr(sandbox_capacity, "get_pool", lambda: _return(pool))

    held = await sandbox_capacity.count_held_sandbox_capacity_leases(provider="ec2")

    assert held == 2
    assert "locked_by IS NOT NULL" in pool.statement
    assert "locked_until > NOW()" in pool.statement
    assert "OR EXISTS" in pool.statement
    assert "wj.status::text = 'RUNNING'" in pool.statement
    assert "FROM sandbox_runs AS run" in pool.statement
    assert "run.terminated_at IS NULL" in pool.statement
    assert pool.args == ("ec2",)
