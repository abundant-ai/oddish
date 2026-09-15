from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from oddish import observability
from oddish.db import WorkerJobKind, WorkerJobStatus


@dataclass
class _Instrument:
    observations: list[tuple[float, dict[str, Any]]] = field(default_factory=list)
    failure: Exception | None = None

    def _observe(self, value: float, attributes: dict[str, Any]) -> None:
        if self.failure is not None:
            raise self.failure
        self.observations.append((value, attributes))

    add = _observe
    record = _observe
    set = _observe


@pytest.fixture
def metric_instruments(monkeypatch):
    instruments: dict[str, _Instrument] = {}
    definitions: dict[str, tuple[str, str]] = {}

    def create(name: str, *, unit: str, description: str) -> _Instrument:
        definitions[name] = (unit, description)
        return instruments.setdefault(name, _Instrument())

    fake_logfire = types.SimpleNamespace(
        metric_counter=create,
        metric_histogram=create,
        metric_gauge=create,
    )
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)
    monkeypatch.setattr(observability, "_configured", True)
    for name in (
        "_worker_job_transitions_counter",
        "_worker_job_duration_histogram",
        "_thunder_capacity_handoffs_counter",
        "_queue_jobs_gauge",
        "_queue_slots_gauge",
        "_dispatch_workers_spawned_counter",
        "_dispatch_cycles_counter",
        "_dispatch_duration_histogram",
    ):
        monkeypatch.setattr(observability, name, None)
    monkeypatch.setattr(observability, "_last_dispatch_queue_keys", set())
    return instruments, definitions


def test_recording_functions_are_noops_when_logfire_is_not_configured(
    monkeypatch,
):
    monkeypatch.setattr(observability, "_configured", False)
    fake_logfire = types.SimpleNamespace(
        metric_counter=lambda *_args, **_kwargs: pytest.fail("created counter"),
        metric_histogram=lambda *_args, **_kwargs: pytest.fail("created histogram"),
        metric_gauge=lambda *_args, **_kwargs: pytest.fail("created gauge"),
    )
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)

    observability.record_worker_job_transition(
        kind=WorkerJobKind.TRIAL,
        outcome=WorkerJobStatus.SUCCESS,
        queue_key="openai/gpt-5",
        execution_lane="default",
        duration_seconds=1.0,
    )
    observability.record_thunder_capacity_handoff(
        outcome="requested", target_environment="modal"
    )
    observability.record_dispatch_snapshot(
        queue_keys=(),
        queued_by_queue={},
        running_by_queue_key={},
        held_by_queue_key={},
        concurrency_limits={},
    )
    observability.record_dispatch_cycle(
        workers_spawned=0,
        spawn_cap_reached=False,
        duration_seconds=1.0,
        outcome="success",
    )


def test_worker_metrics_have_bounded_attributes_and_measured_duration(
    metric_instruments,
):
    instruments, definitions = metric_instruments

    observability.record_worker_job_transition(
        kind=WorkerJobKind.TRIAL,
        outcome=WorkerJobStatus.RETRYING,
        queue_key="openai/gpt-5",
        execution_lane="default",
        duration_seconds=3.5,
    )

    transition = instruments["oddish.worker_job.transitions"].observations
    duration = instruments["oddish.worker_job.duration"].observations
    assert transition == [
        (
            1,
            {
                "kind": "TRIAL",
                "outcome": "RETRYING",
                "queue_key": "openai/gpt-5",
                "execution_lane": "default",
            },
        )
    ]
    assert duration == [(3.5, transition[0][1])]
    assert set(transition[0][1]) == {
        "kind",
        "outcome",
        "queue_key",
        "execution_lane",
    }
    assert definitions["oddish.worker_job.transitions"][0] == "{transition}"
    assert definitions["oddish.worker_job.duration"][0] == "s"


def test_thunder_handoff_metric_has_bounded_attributes(metric_instruments):
    instruments, definitions = metric_instruments

    observability.record_thunder_capacity_handoff(
        outcome="completed", target_environment="modal"
    )

    observations = instruments["oddish.thunder.capacity_handoffs"].observations
    assert observations == [
        (
            1,
            {"outcome": "completed", "target_environment": "modal"},
        )
    ]
    assert definitions["oddish.thunder.capacity_handoffs"][0] == "{handoff}"


def test_dispatch_snapshot_emits_plan_values_and_empty_aggregate_zero(
    metric_instruments,
):
    instruments, _definitions = metric_instruments

    observability.record_dispatch_snapshot(
        queue_keys=("openai/gpt-5",),
        queued_by_queue={"openai/gpt-5": 4},
        running_by_queue_key={"openai/gpt-5": 2},
        held_by_queue_key={"openai/gpt-5": 3},
        concurrency_limits={"openai/gpt-5": 8},
    )
    jobs = instruments["oddish.queue.jobs"].observations
    slots = instruments["oddish.queue.slots"].observations
    assert (4, {"state": "queued", "queue_key": "openai/gpt-5"}) in jobs
    assert (2, {"state": "running", "queue_key": "openai/gpt-5"}) in jobs
    assert (3, {"state": "held", "queue_key": "openai/gpt-5"}) in slots
    assert (8, {"state": "limit", "queue_key": "openai/gpt-5"}) in slots

    observability.record_dispatch_snapshot(
        queue_keys=(),
        queued_by_queue={},
        running_by_queue_key={},
        held_by_queue_key={},
        concurrency_limits={},
    )
    assert jobs[-4:] == [
        (0, {"state": "queued", "queue_key": "__all__"}),
        (0, {"state": "running", "queue_key": "__all__"}),
        (0, {"state": "queued", "queue_key": "openai/gpt-5"}),
        (0, {"state": "running", "queue_key": "openai/gpt-5"}),
    ]
    assert slots[-3:] == [
        (0, {"state": "held", "queue_key": "__all__"}),
        (0, {"state": "limit", "queue_key": "__all__"}),
        (0, {"state": "held", "queue_key": "openai/gpt-5"}),
    ]


def test_dispatch_snapshot_retries_failed_departed_queue_zero(metric_instruments):
    instruments, _definitions = metric_instruments
    queue_key = "openai/gpt-5"

    observability.record_dispatch_snapshot(
        queue_keys=(queue_key,),
        queued_by_queue={queue_key: 4},
        running_by_queue_key={queue_key: 2},
        held_by_queue_key={queue_key: 3},
        concurrency_limits={queue_key: 8},
    )

    jobs = instruments["oddish.queue.jobs"]
    jobs.failure = RuntimeError("export failed")
    observability.record_dispatch_snapshot(
        queue_keys=(),
        queued_by_queue={},
        running_by_queue_key={},
        held_by_queue_key={},
        concurrency_limits={},
    )
    assert observability._last_dispatch_queue_keys == {queue_key}

    jobs.failure = None
    observability.record_dispatch_snapshot(
        queue_keys=(),
        queued_by_queue={},
        running_by_queue_key={},
        held_by_queue_key={},
        concurrency_limits={},
    )
    assert observability._last_dispatch_queue_keys == set()
    assert jobs.observations[-2:] == [
        (0, {"state": "queued", "queue_key": queue_key}),
        (0, {"state": "running", "queue_key": queue_key}),
    ]

    observation_count = len(jobs.observations)
    observability.record_dispatch_snapshot(
        queue_keys=(),
        queued_by_queue={},
        running_by_queue_key={},
        held_by_queue_key={},
        concurrency_limits={},
    )
    assert len(jobs.observations) == observation_count + 2


def test_logfire_observation_failure_does_not_escape(metric_instruments):
    instruments, _definitions = metric_instruments
    observability.record_dispatch_cycle(
        workers_spawned=2,
        spawn_cap_reached=True,
        duration_seconds=0.25,
        outcome="success",
    )
    instruments["oddish.dispatch.cycles"].failure = RuntimeError("export failed")

    observability.record_dispatch_cycle(
        workers_spawned=1,
        spawn_cap_reached=False,
        duration_seconds=0.5,
        outcome="error",
    )

    assert instruments["oddish.dispatch.workers_spawned"].observations == [
        (2, {"outcome": "success", "spawn_cap_reached": True})
    ]
    assert instruments["oddish.dispatch.duration"].observations[-1][0] == 0.5


def test_dispatch_cycle_records_skipped_without_spawn_count(metric_instruments):
    instruments, definitions = metric_instruments

    observability.record_dispatch_cycle(
        workers_spawned=0,
        spawn_cap_reached=False,
        duration_seconds=0.75,
        outcome="skipped",
    )

    attributes = {"outcome": "skipped", "spawn_cap_reached": False}
    assert instruments["oddish.dispatch.cycles"].observations == [(1, attributes)]
    assert instruments["oddish.dispatch.duration"].observations == [(0.75, attributes)]
    assert instruments["oddish.dispatch.workers_spawned"].observations == []
    assert definitions["oddish.dispatch.cycles"][0] == "{cycle}"


def test_dispatch_cycle_records_cancelled_without_spawn_count(metric_instruments):
    instruments, _definitions = metric_instruments

    observability.record_dispatch_cycle(
        workers_spawned=0,
        spawn_cap_reached=True,
        duration_seconds=0.5,
        outcome="cancelled",
    )

    attributes = {"outcome": "cancelled", "spawn_cap_reached": True}
    assert instruments["oddish.dispatch.cycles"].observations == [(1, attributes)]
    assert instruments["oddish.dispatch.duration"].observations == [(0.5, attributes)]
    assert instruments["oddish.dispatch.workers_spawned"].observations == []
