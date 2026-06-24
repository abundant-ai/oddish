from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod
from oddish.core import endpoints
from oddish.db import TaskStatus, TrialStatus


class _Result:
    def __init__(self, scalar=None):
        self._scalar = scalar

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
        self.events.append(("execute", None))
        return _Result(scalar=self.trial)

    async def scalar(self, _statement, _params=None):
        self.events.append(("scalar", None))
        return self.registry_auth_enc

    async def get(self, _model, key):
        self.events.append(("get", key))
        return self.task

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

    # For stuck/live trials, read the credential before the old job cancellation
    # scrubs it so the replacement trial can authenticate.
    assert captured["registry_auth_enc"] == "ENC"
