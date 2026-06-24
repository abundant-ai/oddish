"""Browse-projection dedupe and bulk trial/worker_job insert on task submission.

These tests cover two behaviors of the task create / append path:

* ``recompute_task_browse_projection`` runs **once** on ``create_task`` (the
  post-trial recompute), with the redundant membership-hook recompute (fired
  on incomplete pre-version state) suppressed -- while STILL running on the
  append / standalone-link path.
* Trials and their ``worker_jobs`` rows are inserted with **one**
  ``INSERT ... SELECT unnest(...)`` statement per table (not one per row),
  preserving ``{task_id}-{i}`` ordering, ``validate_payload``, and
  byte-identical row contents versus the per-row construction.

They run against a real Postgres (``ODDISH_DATABASE_URL``).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod  # noqa: E402
from oddish.config import settings  # noqa: E402
from oddish.db import (  # noqa: E402
    TaskModel,
    TrialModel,
    WorkerJobModel,
    get_session,
)
from oddish.queue import (  # noqa: E402
    append_trials_to_task,
    create_task,
    get_or_create_experiment,
)
from oddish.schemas import TaskSubmission, TrialSpec  # noqa: E402

_RUN = uuid.uuid4().hex[:8]


def _submission(
    name: str,
    *,
    n_trials: int,
    experiment_id: str | None = None,
    extra_instructions: str | None = None,
) -> TaskSubmission:
    return TaskSubmission(
        name=name,
        task_path="s3://test-bucket/bulk-insert-fake-task",
        user="test",
        experiment_id=experiment_id,
        extra_instructions=extra_instructions,
        trials=[TrialSpec(agent="nop", model=None) for _ in range(n_trials)],
    )


@pytest_asyncio.fixture
async def cleanup_task_ids():
    ids: list[str] = []
    yield ids
    async with get_session() as s:
        for tid in ids:
            # ON DELETE CASCADE removes trials + task_versions + worker_jobs.
            await s.execute(
                WorkerJobModel.__table__.delete().where(
                    WorkerJobModel.subject_id == tid
                )
            )
            await s.execute(TaskModel.__table__.delete().where(TaskModel.id == tid))


class _ExecuteTracer:
    """Wrap ``session.execute`` to capture rendered statement SQL strings."""

    def __init__(self, session):
        self._session = session
        self._orig = session.execute
        self.statements: list[str] = []
        session.execute = self  # type: ignore[method-assign]

    async def __call__(self, statement, *args, **kwargs):
        self.statements.append(str(statement))
        return await self._orig(statement, *args, **kwargs)

    def count_inserts(self, table: str, *, require: str | None = None) -> int:
        needle = f"insert into {table}"
        return sum(
            1
            for s in self.statements
            if needle in s.lower() and (require is None or require in s.lower())
        )


# ---------------------------------------------------------------------------
# Create-path browse-projection dedupe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompute_called_once_on_create(monkeypatch, cleanup_task_ids):
    """create_task must invoke recompute_task_browse_projection exactly once.

    Today it runs twice: once via the _link_task_to_experiment membership
    hook (on empty pre-version state) and once after trials are inserted.
    """
    calls: list[str] = []
    real = queue_mod.recompute_task_browse_projection

    async def spy(session, *, task_id):
        calls.append(task_id)
        return await real(session, task_id=task_id)

    monkeypatch.setattr(queue_mod, "recompute_task_browse_projection", spy)

    task_id = f"bulk-create-once-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session, _submission("create-once", n_trials=2), task_id=task_id
        )

    assert calls == [task_id], (
        f"expected a single create-path recompute, got {len(calls)}: {calls}"
    )


@pytest.mark.asyncio
async def test_recompute_still_called_on_append(monkeypatch, cleanup_task_ids):
    """The append/standalone-link path MUST still recompute via the hook."""
    task_id = f"bulk-append-recompute-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session, _submission("append-base", n_trials=1), task_id=task_id
        )

    calls: list[str] = []
    real = queue_mod.recompute_task_browse_projection

    async def spy(session, *, task_id):
        calls.append(task_id)
        return await real(session, task_id=task_id)

    monkeypatch.setattr(queue_mod, "recompute_task_browse_projection", spy)

    async with get_session() as session:
        exp = await get_or_create_experiment(session, f"append-exp-{_RUN}", None)
        await session.flush()
        task = await session.get(TaskModel, task_id)
        await append_trials_to_task(
            session,
            task=task,
            submission=_submission("append-more", n_trials=1),
            experiment_id=exp.id,
        )

    assert task_id in calls, "append path must still recompute the projection"


# ---------------------------------------------------------------------------
# Bulk insert of trials + worker_jobs
# ---------------------------------------------------------------------------


async def _trials_for(session, task_id: str) -> list[TrialModel]:
    rows = await session.execute(
        TrialModel.__table__.select()
        .where(TrialModel.task_id == task_id)
        .order_by(TrialModel.id)
    )
    return list(rows.mappings())


async def _trial_worker_jobs_for(session, task_id: str, trial_ids: list[str]):
    rows = await session.execute(
        WorkerJobModel.__table__.select().where(
            WorkerJobModel.subject_id.in_(trial_ids),
            WorkerJobModel.kind == "TRIAL",
        )
    )
    return list(rows.mappings())


@pytest.mark.asyncio
async def test_create_bulk_inserts_n_rows_in_one_statement(cleanup_task_ids):
    """N trials -> N trial rows + N TRIAL worker_jobs, each via ONE statement."""
    n = 4
    task_id = f"bulk-create-n-{_RUN}"
    cleanup_task_ids.append(task_id)

    async with get_session() as session:
        tracer = _ExecuteTracer(session)
        await create_task(session, _submission("create-n", n_trials=n), task_id=task_id)
        # The per-trial fan-out must collapse to a single ``unnest`` statement
        # per table (task-level TAG_PROJECT/TASK_EXPAND enqueues are separate).
        trial_inserts = tracer.count_inserts("trials", require="unnest")
        wj_inserts = tracer.count_inserts("worker_jobs", require="unnest")

    async with get_session() as session:
        trials = await _trials_for(session, task_id)
        trial_ids = [t["id"] for t in trials]
        wjs = await _trial_worker_jobs_for(session, task_id, trial_ids)

    assert len(trials) == n
    assert trial_ids == [f"{task_id}-{i}" for i in range(n)], "index order preserved"
    assert len(wjs) == n
    assert {w["subject_id"] for w in wjs} == set(trial_ids)
    assert trial_inserts == 1, f"expected ONE trials insert, got {trial_inserts}"
    assert wj_inserts == 1, f"expected ONE worker_jobs insert, got {wj_inserts}"


@pytest.mark.asyncio
async def test_append_bulk_inserts_in_one_statement(cleanup_task_ids):
    """append_trials_to_task uses one bulk insert per table and returns trials."""
    task_id = f"bulk-append-n-{_RUN}"
    cleanup_task_ids.append(task_id)
    async with get_session() as session:
        await create_task(
            session, _submission("append-n-base", n_trials=1), task_id=task_id
        )

    n = 3
    async with get_session() as session:
        exp = await get_or_create_experiment(session, f"append-n-exp-{_RUN}", None)
        await session.flush()
        task = await session.get(TaskModel, task_id)
        tracer = _ExecuteTracer(session)
        new_trials = await append_trials_to_task(
            session,
            task=task,
            submission=_submission("append-n", n_trials=n),
            experiment_id=exp.id,
        )
        trial_inserts = tracer.count_inserts("trials", require="unnest")
        wj_inserts = tracer.count_inserts("worker_jobs", require="unnest")
        new_ids = [t.id for t in new_trials]

    # base task had 1 trial (index 0); appended trials are indices 1..n
    assert new_ids == [f"{task_id}-{i}" for i in range(1, n + 1)]
    assert trial_inserts == 1, f"expected ONE trials insert, got {trial_inserts}"
    assert wj_inserts == 1, f"expected ONE worker_jobs insert, got {wj_inserts}"

    async with get_session() as session:
        wjs = await _trial_worker_jobs_for(session, task_id, new_ids)
    assert {w["subject_id"] for w in wjs} == set(new_ids)


@pytest.mark.asyncio
async def test_bulk_insert_parity_with_per_row(cleanup_task_ids):
    """The bulk path produces byte-identical rows to a faithful per-row replica.

    Both tasks share one experiment + identical specs; we compare every
    semantically meaningful column (excluding the task-id-prefixed ids and
    server timestamps, which differ by construction).
    """
    n = 3
    bulk_id = f"parity-bulk-{_RUN}"
    perrow_id = f"parity-perrow-{_RUN}"
    cleanup_task_ids.extend([bulk_id, perrow_id])

    async with get_session() as session:
        exp = await get_or_create_experiment(session, f"parity-exp-{_RUN}", None)
        await session.flush()
        exp_id = exp.id

        # New bulk path.
        await create_task(
            session,
            _submission(
                "parity-bulk",
                n_trials=n,
                experiment_id=exp_id,
                extra_instructions="probe me",
            ),
            task_id=bulk_id,
        )
        # Faithful replica of the OLD per-row construction (queue.py:658-694).
        await _legacy_per_row_create(
            session,
            perrow_id,
            exp_id,
            _submission(
                "parity-perrow",
                n_trials=n,
                experiment_id=exp_id,
                extra_instructions="probe me",
            ),
        )

    async with get_session() as session:
        bulk_trials = await _trials_for(session, bulk_id)
        perrow_trials = await _trials_for(session, perrow_id)
        bulk_wjs = await _trial_worker_jobs_for(
            session, bulk_id, [t["id"] for t in bulk_trials]
        )
        perrow_wjs = await _trial_worker_jobs_for(
            session, perrow_id, [t["id"] for t in perrow_trials]
        )

    trial_cols = [
        "name",
        "agent",
        "provider",
        "queue_key",
        "model",
        "timeout_minutes",
        "environment",
        "harbor_config",
        "is_probe",
        "max_attempts",
        "status",
        "attempts",
        "origin",
        "experiment_id",
        "org_id",
    ]
    assert len(bulk_trials) == len(perrow_trials) == n
    for b, p in zip(bulk_trials, perrow_trials):
        # ids differ only by task prefix; suffix index must match
        assert b["id"].rsplit("-", 1)[1] == p["id"].rsplit("-", 1)[1]
        assert (
            b["task_version_id"].rsplit("-", 1)[1]
            == p["task_version_id"].rsplit("-", 1)[1]
        )
        for col in trial_cols:
            bv, pv = b[col], p[col]
            if col == "name":  # name is "{task_name}-{i}"; compare suffix
                bv, pv = bv.rsplit("-", 1)[1], pv.rsplit("-", 1)[1]
            assert bv == pv, f"trial column {col} mismatch: {bv!r} != {pv!r}"

    # ``payload``/``subject_id`` carry the task-prefixed trial id (differs by
    # construction); their structural equality is checked via the suffix below.
    wj_cols = [
        "kind",
        "queue_key",
        "subject_table",
        "max_attempts",
        "status",
        "priority",
        "attempts",
    ]
    bulk_wjs.sort(key=lambda w: w["subject_id"])
    perrow_wjs.sort(key=lambda w: w["subject_id"])
    assert len(bulk_wjs) == len(perrow_wjs) == n
    for b, p in zip(bulk_wjs, perrow_wjs):
        for col in wj_cols:
            assert b[col] == p[col], f"worker_job column {col} mismatch"
        # payload carries the index-aligned trial id
        assert b["payload"]["trial_id"] == b["subject_id"]


async def _legacy_per_row_create(session, task_id, experiment_id, submission):
    """Reproduce the original per-row trial/worker_job insert loop verbatim."""
    from oddish.db import TaskVersionModel
    from oddish.queue import enqueue_trial_worker_job

    task_name = submission.name
    task = TaskModel(
        id=task_id,
        name=task_name,
        user="test",
        task_path=submission.task_path,
        task_s3_key="legacy/key",
    )
    session.add(task)
    await session.flush()
    version_id = f"{task_id}-v1"
    session.add(
        TaskVersionModel(
            id=version_id,
            task_id=task_id,
            version=1,
            task_path=submission.task_path,
            task_s3_key="legacy/key",
        )
    )
    await session.flush()
    task.current_version_id = version_id
    await queue_mod._link_task_to_experiment(
        session, task_id=task_id, experiment_id=experiment_id
    )

    for i, spec in enumerate(submission.trials):
        model = settings.normalize_trial_model(spec.agent, spec.model)
        provider = settings.get_provider_for_trial(spec.agent, model)
        queue_key = settings.get_queue_key_for_trial(spec.agent, model)
        trial_id = f"{task_id}-{i}"
        harbor_config = queue_mod._build_harbor_config_for_trial(submission, spec)
        trial = TrialModel(
            id=trial_id,
            name=f"{task_name}-{i}",
            task_id=task_id,
            task_version_id=version_id,
            experiment_id=experiment_id,
            org_id=None,
            agent=spec.agent,
            provider=provider,
            queue_key=queue_key,
            model=model,
            timeout_minutes=spec.timeout_minutes,
            environment=spec.environment,
            harbor_config=harbor_config,
            is_probe=(harbor_config or {}).get("mode") == "probe",
            max_attempts=submission.max_trial_attempts,
            status=queue_mod.TrialStatus.QUEUED,
        )
        session.add(trial)
        await enqueue_trial_worker_job(
            session,
            trial_id=trial_id,
            queue_key=queue_key,
            org_id=None,
            max_attempts=trial.max_attempts,
        )
    await session.flush()
