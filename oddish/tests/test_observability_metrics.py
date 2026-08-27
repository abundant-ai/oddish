from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field
from contextlib import nullcontext
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
        span=lambda *_args, **_kwargs: nullcontext(),
    )
    monkeypatch.setitem(sys.modules, "logfire", fake_logfire)
    monkeypatch.setattr(observability, "_configured", True)
    for name in (
        "_worker_job_transitions_counter",
        "_worker_job_duration_histogram",
        "_queue_jobs_gauge",
        "_queue_slots_gauge",
        "_dispatch_workers_spawned_counter",
        "_dispatch_cycles_counter",
        "_dispatch_duration_histogram",
        "_analysis_stage_duration_histogram",
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
    observability.record_analysis_stage_duration(
        analysis_kind="qa",
        stage="prepare",
        outcome="success",
        queue_key="anthropic/claude-sonnet-4-5",
        target_count=7,
        retried=False,
        source="worker",
        duration_seconds=1.0,
    )


@pytest.mark.parametrize(
    ("target_count", "expected"),
    [
        (-1, "0"),
        (0, "0"),
        (1, "1"),
        (2, "2-5"),
        (5, "2-5"),
        (6, "6-10"),
        (10, "6-10"),
        (11, "11-25"),
        (25, "11-25"),
        (26, "26-50"),
        (50, "26-50"),
        (51, "51+"),
    ],
)
def test_analysis_target_bucket_is_bounded(target_count, expected):
    assert observability.analysis_target_bucket(target_count) == expected


def test_analysis_stage_metric_has_only_bounded_attributes(metric_instruments):
    instruments, definitions = metric_instruments

    observability.record_analysis_stage_duration(
        analysis_kind="qa",
        stage="agent_execution",
        outcome="success",
        queue_key="anthropic/claude-sonnet-4-5",
        target_count=7,
        retried=True,
        source="worker",
        duration_seconds=12.5,
    )

    assert instruments["oddish.analysis.stage.duration"].observations == [
        (
            12.5,
            {
                "analysis_kind": "qa",
                "stage": "agent_execution",
                "outcome": "success",
                "queue_key": "anthropic/claude-sonnet-4-5",
                "target_bucket": "6-10",
                "retried": True,
                "source": "worker",
            },
        )
    ]
    assert definitions["oddish.analysis.stage.duration"][0] == "s"


def test_analysis_stage_context_records_error_and_keeps_ids_on_span_only(
    metric_instruments, monkeypatch
):
    instruments, _definitions = metric_instruments
    opened_spans: list[tuple[str, dict[str, Any]]] = []

    def open_span(name: str, **attributes):
        opened_spans.append((name, attributes))
        return nullcontext()

    monkeypatch.setattr(observability, "span", open_span)
    telemetry = observability.AnalysisTelemetry(
        analysis_kind="summarize",
        queue_key="openai/gpt-5",
        target_count=1,
        retried=False,
        source="cleanup",
        trial_id="trial-123",
        task_id="task-456",
        attempt=1,
        worker_job_id="job-789",
    )

    with pytest.raises(RuntimeError, match="import failed"):
        with telemetry.stage("import"):
            raise RuntimeError("import failed")

    metric_attributes = instruments[
        "oddish.analysis.stage.duration"
    ].observations[0][1]
    assert metric_attributes["outcome"] == "error"
    assert set(metric_attributes) == {
        "analysis_kind",
        "stage",
        "outcome",
        "queue_key",
        "target_bucket",
        "retried",
        "source",
    }
    assert opened_spans == [
        (
            "analysis.import",
            {
                "analysis_kind": "summarize",
                "analysis_stage": "import",
                "analysis_target_count": 1,
                "queue_key": "openai/gpt-5",
                "retried": False,
                "trial_id": "trial-123",
                "task_id": "task-456",
                "worker_job_id": "job-789",
                "attempt": 1,
                "source": "cleanup",
            },
        )
    ]


def test_unknown_analysis_stage_is_not_exported(metric_instruments, caplog):
    instruments, _definitions = metric_instruments

    observability.record_analysis_stage_duration(
        analysis_kind="qa",
        stage="trial-123",
        outcome="success",
        queue_key="openai/gpt-5",
        target_count=5,
        retried=False,
        source="worker",
        duration_seconds=1.0,
    )

    assert "oddish.analysis.stage.duration" not in instruments
    assert "refusing unknown analysis metric dimensions" in caplog.text


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
