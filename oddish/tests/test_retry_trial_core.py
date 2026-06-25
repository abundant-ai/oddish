from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from harbor.trial.hooks import TrialEvent

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod
from oddish.core import endpoints
import oddish.core.endpoints.trials as trials_endpoint_mod
import oddish.workers.queue.trial_handler as trial_handler_mod
from oddish.db import TaskStatus, TrialModel, TrialStatus
from oddish.schemas import RegistryAuth
from oddish.workers.harbor.outcome import HarborOutcome


class _Result:
    def __init__(self, scalar=None, rowcount=0):
        self._scalar = scalar
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._scalar


class _RecordingTrial:
    def __init__(self, events, *, status=TrialStatus.SUCCESS, error_message=None):
        self._events = events
        self._superseded_by_trial_id = None
        self.id = "task-1-0"
        self.name = "task-1-0"
        self.task_id = "task-1"
        self.task_version_id = "task-1-v1"
        self.experiment_id = "exp-1"
        self.org_id = "org-1"
        self.agent = "codex"
        self.provider = "openai"
        self.queue_key = "openai/gpt-5"
        self.model = "gpt-5"
        self.timeout_minutes = None
        self.environment = None
        self.harbor_config = None
        self.is_probe = False
        self.max_attempts = 6
        self.status = status
        self.error_message = error_message
        self.harbor_stage = None
        self.finished_at = None
        self.current_worker_id = None
        self.current_queue_slot = None

    @property
    def superseded_by_trial_id(self):
        return self._superseded_by_trial_id

    @superseded_by_trial_id.setter
    def superseded_by_trial_id(self, value):
        self._events.append(("supersede", value))
        self._superseded_by_trial_id = value


class _RecordingSession:
    def __init__(self, *, trial, task, events, registry_auth_enc=None):
        self.trial = trial
        self.task = task
        self.events = events
        self.registry_auth_enc = registry_auth_enc
        self.added = []

    async def execute(self, _statement, _params=None):
        sql = str(_statement)
        self.events.append(("execute", sql))
        if "UPDATE trials" in sql and "superseded_by_trial_id IS NULL" in sql:
            if self.trial.superseded_by_trial_id is not None:
                return _Result(rowcount=0)
            terminal = self.trial.status in {TrialStatus.FAILED, TrialStatus.SUCCESS}
            self.trial.superseded_by_trial_id = _params["new_trial_id"]
            if not terminal:
                self.trial.status = TrialStatus.FAILED
                self.trial.error_message = (
                    self.trial.error_message or "Superseded by user retry"
                )
                self.trial.current_worker_id = None
                self.trial.current_queue_slot = None
            return _Result(rowcount=1)
        return _Result(scalar=self.trial)

    def expire(self, _obj):
        self.events.append(("expire", None))

    async def scalar(self, _statement, _params=None):
        self.events.append(("scalar", None))
        return self.registry_auth_enc

    async def get(self, _model, key, **kwargs):
        self.events.append(("get", key, kwargs))
        return self.trial if _model is TrialModel else self.task

    def add(self, obj):
        self.events.append(("add", obj.id))
        self.added.append(obj)

    async def flush(self):
        self.events.append(("flush", None))
        assert self.added
        assert self.trial.superseded_by_trial_id is None

    async def commit(self):
        self.events.append(("commit", None))


@pytest.mark.asyncio
async def test_retry_trial_flushes_new_trial_before_setting_superseded_fk(
    monkeypatch,
):
    events = []
    trial = _RecordingTrial(events)
    task = SimpleNamespace(
        id="task-1",
        name="task-1",
        status=TaskStatus.COMPLETED,
        finished_at=None,
    )
    session = _RecordingSession(trial=trial, task=task, events=events)

    async def fake_reserve_next_trial_index(_session, *, task_id):
        events.append(("reserve_next_index", task_id))
        return 1

    async def fake_enqueue_trial_worker_job(
        _session,
        *,
        trial_id,
        queue_key,
        org_id,
        max_attempts,
        parent_job_id=None,
        harbor_variant_id="default",
        registry_auth_enc=None,
    ):
        events.append(("enqueue", trial_id, queue_key, org_id, max_attempts))

    monkeypatch.setattr(
        queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index
    )
    monkeypatch.setattr(
        queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job
    )

    result = await endpoints.retry_trial_core(
        session, trial_id=trial.id, org_id="org-1"
    )

    assert result == {
        "status": "queued",
        "trial_id": "task-1-1",
        "superseded_trial_id": "task-1-0",
    }
    event_names = [event[0] for event in events]
    assert event_names.index("add") < event_names.index("flush")
    assert event_names.index("flush") < event_names.index("supersede")
    assert any("kind::text IN ('QA', 'VERDICT')" in str(event[1]) for event in events)


@pytest.mark.asyncio
async def test_retry_lost_race_raises_409_and_does_not_enqueue(monkeypatch):
    events = []
    trial = _RecordingTrial(events, status=TrialStatus.SUCCESS)
    task = SimpleNamespace(
        id="task-1", name="task-1", status=TaskStatus.COMPLETED, finished_at=None
    )
    session = _RecordingSession(trial=trial, task=task, events=events)

    async def fake_reserve_next_trial_index(_session, *, task_id):
        return 1

    enqueued = []

    async def fake_enqueue_trial_worker_job(_session, *, trial_id, **_):
        enqueued.append(trial_id)

    monkeypatch.setattr(
        queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index
    )
    monkeypatch.setattr(
        queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job
    )

    real_execute = session.execute

    async def racing_execute(_statement, _params=None):
        sql = str(_statement)
        if "UPDATE trials" in sql and "superseded_by_trial_id IS NULL" in sql:
            trial._superseded_by_trial_id = "task-1-99"
        return await real_execute(_statement, _params)

    session.execute = racing_execute

    with pytest.raises(HTTPException) as exc:
        await endpoints.retry_trial_core(session, trial_id=trial.id, org_id="org-1")

    assert exc.value.status_code == 409
    assert enqueued == []
    assert ("commit", None) not in events
    assert trial.superseded_by_trial_id == "task-1-99"


@pytest.mark.asyncio
async def test_retry_carries_registry_auth_to_new_trial(monkeypatch):
    events = []
    trial = _RecordingTrial(events, status=TrialStatus.RUNNING, error_message="stuck")
    task = SimpleNamespace(
        id="task-1", name="task-1", status=TaskStatus.RUNNING, finished_at=None
    )
    session = _RecordingSession(
        trial=trial, task=task, events=events, registry_auth_enc="ENC"
    )
    captured = {}

    async def fake_reserve_next_trial_index(_session, *, task_id):
        return 1

    async def fake_enqueue_trial_worker_job(_session, *, registry_auth_enc=None, **_):
        captured["registry_auth_enc"] = registry_auth_enc

    monkeypatch.setattr(
        queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index
    )
    monkeypatch.setattr(
        queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job
    )

    await endpoints.retry_trial_core(session, trial_id=trial.id, org_id="org-1")

    assert captured["registry_auth_enc"] == "ENC"


@pytest.mark.asyncio
async def test_retry_moves_verdict_pending_task_back_to_running(monkeypatch):
    events = []
    trial = _RecordingTrial(events, status=TrialStatus.SUCCESS)
    task = SimpleNamespace(
        id="task-1",
        name="task-1",
        status=TaskStatus.VERDICT_PENDING,
        finished_at=object(),
    )
    session = _RecordingSession(trial=trial, task=task, events=events)

    async def fake_reserve_next_trial_index(_session, *, task_id):
        return 1

    async def fake_enqueue_trial_worker_job(_session, **_):
        return None

    monkeypatch.setattr(
        queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index
    )
    monkeypatch.setattr(
        queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job
    )

    await endpoints.retry_trial_core(session, trial_id=trial.id, org_id="org-1")

    assert task.status == TaskStatus.RUNNING
    assert task.finished_at is None


@pytest.mark.asyncio
async def test_retry_uses_fresh_registry_auth_when_supplied(monkeypatch):
    events = []
    trial = _RecordingTrial(events, status=TrialStatus.FAILED)
    task = SimpleNamespace(
        id="task-1", name="task-1", status=TaskStatus.RUNNING, finished_at=None
    )
    session = _RecordingSession(
        trial=trial, task=task, events=events, registry_auth_enc=None
    )
    captured = {}

    async def fake_reserve_next_trial_index(_session, *, task_id):
        return 1

    async def fake_enqueue_trial_worker_job(_session, *, registry_auth_enc=None, **_):
        captured["registry_auth_enc"] = registry_auth_enc

    def fake_encrypt_credentials(creds):
        captured["creds"] = creds
        return "FRESH_ENC"

    monkeypatch.setattr(
        queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index
    )
    monkeypatch.setattr(
        queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job
    )
    monkeypatch.setattr(
        trials_endpoint_mod, "encrypt_credentials", fake_encrypt_credentials
    )

    await endpoints.retry_trial_core(
        session,
        trial_id=trial.id,
        org_id="org-1",
        registry_auth=[
            RegistryAuth(username="alice", token="fresh", registry="ghcr.io")
        ],
    )

    assert captured["registry_auth_enc"] == "FRESH_ENC"
    assert captured["creds"][0].token == "fresh"
    assert ("scalar", None) not in events


@pytest.mark.asyncio
async def test_harbor_event_ignores_superseded_trial(monkeypatch):
    events = []
    trial = _RecordingTrial(events, status=TrialStatus.RUNNING)
    trial.superseded_by_trial_id = "task-1-1"

    @asynccontextmanager
    async def fake_trial_session(
        _trial_id, *, allow_missing=False, with_for_update=False
    ):
        yield SimpleNamespace(execute=lambda *_args, **_kwargs: None), trial

    monkeypatch.setattr(trial_handler_mod, "_trial_session", fake_trial_session)

    await trial_handler_mod._handle_harbor_event(
        SimpleNamespace(
            event=TrialEvent.START,
            environment=None,
            environment_external_id=None,
            environment_provider=None,
        ),
        trial_id=trial.id,
    )

    assert trial.harbor_stage is None


@pytest.mark.asyncio
async def test_store_trial_results_ignores_superseded_trial(monkeypatch):
    events = []
    trial = _RecordingTrial(events, status=TrialStatus.RUNNING)
    trial.superseded_by_trial_id = "task-1-1"
    trial.reward = None
    trial.trial_s3_key = None

    @asynccontextmanager
    async def fake_trial_session(
        _trial_id, *, allow_missing=False, with_for_update=False
    ):
        yield SimpleNamespace(), trial

    monkeypatch.setattr(trial_handler_mod, "_trial_session", fake_trial_session)

    await trial_handler_mod._store_trial_results(
        trial_id=trial.id,
        outcome=HarborOutcome(
            reward=1.0,
            error=None,
            exit_code=0,
            duration_sec=1.0,
            job_result_path=None,
            job_dir=None,
        ),
        trial_s3_key="new-key",
        execution_error=None,
    )

    assert trial.status == TrialStatus.RUNNING
    assert trial.reward is None
    assert trial.trial_s3_key is None


@pytest.mark.asyncio
async def test_prepare_trial_run_ignores_superseded_trial(monkeypatch):
    events = []
    trial = _RecordingTrial(events, status=TrialStatus.FAILED, error_message="old")
    trial.superseded_by_trial_id = "task-1-1"
    trial.attempts = 2

    @asynccontextmanager
    async def fake_trial_session(
        _trial_id, *, allow_missing=False, with_for_update=False
    ):
        yield SimpleNamespace(), trial

    monkeypatch.setattr(trial_handler_mod, "_trial_session", fake_trial_session)

    prepared = await trial_handler_mod._prepare_trial_run(
        trial_id=trial.id,
        worker_id="worker-1",
        queue_slot=1,
        modal_function_call_id="fc-1",
    )

    assert prepared is None
    assert trial.status == TrialStatus.FAILED
    assert trial.error_message == "old"
    assert trial.attempts == 2
    assert trial.current_worker_id is None


@pytest.mark.asyncio
async def test_heartbeat_ignores_superseded_trial(monkeypatch):
    events = []
    trial = _RecordingTrial(events, status=TrialStatus.RUNNING)
    trial.superseded_by_trial_id = "task-1-1"

    @asynccontextmanager
    async def fake_trial_session(
        _trial_id, *, allow_missing=False, with_for_update=False
    ):
        assert with_for_update is True
        yield SimpleNamespace(), trial

    monkeypatch.setattr(trial_handler_mod, "_trial_session", fake_trial_session)

    await trial_handler_mod._touch_trial_execution(
        trial_id=trial.id,
        worker_id="worker-1",
        queue_slot=1,
    )

    assert trial.current_worker_id is None
    assert trial.current_queue_slot is None
