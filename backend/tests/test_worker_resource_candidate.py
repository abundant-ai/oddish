from contextlib import nullcontext

import pytest

from oddish.costs.recorder import WorkerBillingSpec
from worker import functions as workers


@pytest.mark.asyncio
async def test_candidate_entry_uses_shared_execution_with_actual_resources(monkeypatch):
    seen = []

    async def run(*args, **kwargs):
        seen.append((args, kwargs))

    monkeypatch.setattr(workers, "_run_one_job", run)
    await workers.process_single_job_candidate.get_raw_f()(
        "m", priority_class=False, org_id="org", reservation_token="reserved"
    )
    _, kwargs = seen[0]
    assert kwargs["resource_candidate"] is True
    assert kwargs["worker_billing_spec"] == WorkerBillingSpec(
        0.6, 3072, True, cpu_limit=17, configuration="candidate-cpu0.6-mem3072"
    )
    assert kwargs["org_id"] == "org" and kwargs["reservation_token"] == "reserved"
    for args in (
        {},
        {"priority_class": True},
        {"harbor_variant_id": "ephemeral", "priority_class": False},
        {"execution_lane": "ec2_trial", "priority_class": False},
    ):
        with pytest.raises(ValueError):
            await workers.process_single_job_candidate.get_raw_f()("m", **args)


@pytest.mark.asyncio
async def test_shared_execution_forwards_candidate_billing_and_releases_slot(
    monkeypatch,
):
    seen, released = [], []

    async def noop(*args, **kwargs):
        pass

    async def limit(key):
        return 2

    async def acquire(**kwargs):
        return 1

    async def release(**kwargs):
        released.append(kwargs)

    async def drain(key, **kwargs):
        seen.append(kwargs)
        return 2

    monkeypatch.setattr(workers, "_otel_span", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(
        workers.modal, "current_function_call_id", lambda: "fc-candidate"
    )
    monkeypatch.setattr(workers, "configure_storage_paths", noop)
    monkeypatch.setattr(workers, "close_database_connections", noop)
    monkeypatch.setattr(workers, "_effective_model_concurrency", limit)
    monkeypatch.setattr(workers, "acquire_queue_slot", acquire)
    monkeypatch.setattr(workers, "release_queue_slot", release)
    monkeypatch.setattr(workers, "drain_worker_jobs", drain)
    spec = WorkerBillingSpec(
        0.6, 1536, True, cpu_limit=17, configuration="candidate-cpu0.6-mem1536"
    )
    await workers._run_one_job(
        "m",
        resource_candidate=True,
        worker_billing_spec=spec,
        priority_class=False,
        org_id="org",
    )
    assert seen[0]["worker_billing_spec"] is spec
    assert seen[0]["resource_candidate"] is True
    assert seen[0]["modal_function_call_id"] == "fc-candidate"
    assert seen[0]["org_id"] == "org" and seen[0]["priority_class"] is False
    assert len(released) == 1


@pytest.mark.asyncio
async def test_operator_control_reads_by_default_and_stops_without_redeploy(
    monkeypatch,
):
    import worker_resource_rollout as operator
    from oddish.workers.queue import worker_job_single_job as runner

    queries = []

    class Connection:
        async def execute(self, sql, *args):
            queries.append((sql, args))

        async def fetchrow(self, sql):
            return dict(
                id=1,
                fraction=0,
                max_workers=2,
                configuration=operator.WORKER_CANDIDATE_CONFIGURATION,
            )

        async def close(self):
            pass

    async def connect():
        return Connection()

    monkeypatch.setattr(runner, "_open_connection", connect)
    assert (await operator.control.get_raw_f()())["fraction"] == 0
    assert queries == []
    await operator.control.get_raw_f()(0)
    assert queries[0][1] == (0, None, operator.WORKER_CANDIDATE_CONFIGURATION)
    queries.clear()
    for fraction, cap in (
        (-1, 2),
        (1.01, 2),
        (float("nan"), 2),
        (float("inf"), 2),
        (0.01, 3),
        (0.01, -1),
        (None, 2),
    ):
        with pytest.raises(ValueError):
            await operator.control.get_raw_f()(fraction, cap)
    assert queries == []
