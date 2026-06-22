"""create_task must set TrialModel.is_probe from the probe marker so the
indexed column tracks harbor_config.mode."""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.db import TaskModel, TrialModel, get_session  # noqa: E402
from oddish.queue import append_trials_to_task, create_task  # noqa: E402
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402

_RUN = uuid.uuid4().hex[:8]


@pytest_asyncio.fixture
async def cleanup_task_ids():
    ids: list[str] = []
    yield ids
    async with get_session() as s:
        for tid in ids:
            # ON DELETE CASCADE removes trials + task_versions with the task.
            await s.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))


def _submission(*, name: str, extra_instructions: str | None) -> TaskSubmission:
    # An s3:// task_path skips create_task's local upload + timeout validation,
    # so the test needs no real storage and no task.toml on disk.
    return TaskSubmission(
        name=f"{name}-{_RUN}",
        task_path="s3://test-bucket/is-probe-fake-task",
        user="test",
        trials=[TrialSpec(agent="nop", model=None)],
        extra_instructions=extra_instructions,
    )


@pytest.mark.asyncio
async def test_create_task_marks_probe_trials(cleanup_task_ids):
    async with get_session() as session:
        probe_task = await create_task(
            session,
            _submission(name="is-probe-test-probe", extra_instructions="poke around"),
        )
        normal_task = await create_task(
            session, _submission(name="is-probe-test-normal", extra_instructions=None)
        )
    cleanup_task_ids.extend([probe_task.id, normal_task.id])

    async with get_session() as session:
        probe_trials = (
            await session.execute(
                TrialModel.__table__.select().where(TrialModel.task_id == probe_task.id)
            )
        ).all()
        normal_trials = (
            await session.execute(
                TrialModel.__table__.select().where(
                    TrialModel.task_id == normal_task.id
                )
            )
        ).all()

    assert probe_trials and all(row.is_probe for row in probe_trials)
    assert normal_trials and all(not row.is_probe for row in normal_trials)


def test_trial_response_exposes_is_probe():
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from oddish.core.helpers import build_compact_trial_response

    _now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    trial = SimpleNamespace(
        id="t-1",
        name="t-1",
        task_id="task-1",
        task_version_id=None,
        experiment_id="exp-1",
        agent="nop",
        provider="local",
        queue_key="nop",
        model=None,
        status="queued",
        origin="oddish",
        attempts=0,
        max_attempts=1,
        harbor_stage=None,
        reward=None,
        error_message=None,
        result=None,
        harbor_config={"mode": "probe"},
        harbor_sha=None,
        is_probe=True,
        input_tokens=None,
        cache_tokens=None,
        output_tokens=None,
        cost_usd=None,
        phase_timing=None,
        has_trajectory=False,
        analysis=None,
        analysis_status=None,
        analysis_error=None,
        superseded_by_trial_id=None,
        created_at=_now,
        started_at=None,
        finished_at=None,
    )

    response = build_compact_trial_response(trial, task_path="tasks/task-1")
    assert response.is_probe is True


@pytest.mark.asyncio
async def test_list_task_trials_filters_by_probe(cleanup_task_ids):
    from oddish.core.public_helpers import list_task_trials_for_task

    # create_task seeds one probe trial; append_trials_to_task adds one normal
    # trial to the SAME task, in the same session. append takes the TaskModel
    # via the keyword-only `task=` param (not task_id).
    async with get_session() as session:
        task = await create_task(
            session, _submission(name="is-probe-filter", extra_instructions="poke")
        )
        await append_trials_to_task(
            session,
            task=task,
            submission=_submission(name="is-probe-filter", extra_instructions=None),
        )
    cleanup_task_ids.append(task.id)

    async with get_session() as session:
        only_probes = await list_task_trials_for_task(session, task.id, probe=True)
        only_normal = await list_task_trials_for_task(session, task.id, probe=False)
        everything = await list_task_trials_for_task(session, task.id)

    assert only_probes and all(t.is_probe for t in only_probes)
    assert only_normal and all(not t.is_probe for t in only_normal)
    assert len(everything) == len(only_probes) + len(only_normal)


# ---------------------------------------------------------------------------
# Fix 2 — combine tripwire (pure in-memory, no DB required)
# ---------------------------------------------------------------------------


def test_combine_fields_include_is_probe():
    from oddish.core.endpoints import _COMBINE_TRIAL_RESULT_FIELDS

    assert "is_probe" in _COMBINE_TRIAL_RESULT_FIELDS


# ---------------------------------------------------------------------------
# Fix 1 — retry path preserves is_probe (monkeypatched, no external services)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_preserves_is_probe(monkeypatch):
    """retry_trial_core must copy is_probe from the source trial to the new one."""
    import sys
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

    import oddish.queue as queue_mod
    from oddish.core import endpoints
    from oddish.db import TaskStatus, TrialStatus

    class _Result:
        def __init__(self, scalar=None):
            self._scalar = scalar

        def scalar_one_or_none(self):
            return self._scalar

    class _ProbeTrial:
        """A probe trial stub — is_probe=True."""

        def __init__(self):
            self._superseded_by_trial_id = None
            self.id = "task-probe-0"
            self.name = "task-probe-0"
            self.task_id = "task-probe"
            self.task_version_id = "task-probe-v1"
            self.experiment_id = "exp-probe"
            self.org_id = None
            self.agent = "nop"
            self.provider = "local"
            self.queue_key = "nop"
            self.model = None
            self.timeout_minutes = None
            self.environment = None
            self.harbor_config = {"mode": "probe"}
            self.is_probe = True
            self.max_attempts = 1
            self.status = TrialStatus.FAILED
            self.error_message = "test failure"
            self.harbor_stage = None
            self.finished_at = None
            self.current_worker_id = None
            self.current_queue_slot = None

        @property
        def superseded_by_trial_id(self):
            return self._superseded_by_trial_id

        @superseded_by_trial_id.setter
        def superseded_by_trial_id(self, value):
            self._superseded_by_trial_id = value

    task = SimpleNamespace(
        id="task-probe",
        name="task-probe",
        status=TaskStatus.RUNNING,
        finished_at=None,
    )
    trial = _ProbeTrial()
    added: list = []

    class _Session:
        async def execute(self, _stmt, _params=None):
            return _Result(scalar=trial)

        async def get(self, _model, _key):
            return task

        def add(self, obj):
            added.append(obj)

        async def flush(self):
            pass

        async def commit(self):
            pass

    async def fake_reserve(session, *, task_id):
        return 1

    async def fake_enqueue(
        session,
        *,
        trial_id,
        queue_key,
        org_id,
        max_attempts,
        parent_job_id=None,
        harbor_variant_id="default",
    ):
        pass

    monkeypatch.setattr(queue_mod, "reserve_next_trial_index", fake_reserve)
    monkeypatch.setattr(queue_mod, "enqueue_trial_worker_job", fake_enqueue)

    await endpoints.retry_trial_core(_Session(), trial_id=trial.id, org_id=None)

    assert added, "retry_trial_core should have added a new TrialModel"
    new_trial = added[0]
    assert (
        new_trial.is_probe is True
    ), f"Expected is_probe=True on retried trial, got {new_trial.is_probe!r}"
