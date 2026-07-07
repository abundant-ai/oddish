"""Settle sweep for cancelled trials: measured usage from salvaged S3
artifacts replaces the flat ``unpriced_trial_cost_usd`` floor, and deferred
sandbox handles of cancelled TRIAL jobs are reaped after the harvest grace.

The settle pass must only ever touch usage columns -- status/error/
finished_at stay exactly as the cancel wrote them -- and must be idempotent
(a settled row exits candidacy).
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.workers.queue.cleanup as cleanup  # noqa: E402
from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
    WorkerJobKind,
    WorkerJobModel,
    WorkerJobStatus,
    utcnow,
)
from oddish.workers.queue.cleanup import (  # noqa: E402
    _salvaged_usage_from_artifacts,
    reap_cancelled_trial_sandboxes,
    settle_cancelled_trial_costs,
)

PREFIX = "tasks/task-x/trials/task-x-0/"


def _result_json(**agent_result) -> str:
    return json.dumps({"trial_name": "t", "agent_result": agent_result})


def _with_sentinel(prefix: str, files: dict[str, str]) -> dict[str, str]:
    """A completed harvest: the upload sentinel plus its artifacts."""
    return {f"{prefix}harvest-complete.json": "{}", **files}


class _FakeStorage:
    """Minimal StorageClient stand-in. ``mtimes``/``sizes`` map S3 key ->
    metadata so tests can exercise newest-attempt selection and size caps.
    """

    def __init__(self, files: dict[str, str], *, mtimes=None, sizes=None):
        self.files = files
        self.mtimes = mtimes or {}
        self.sizes = sizes or {}
        self.download_calls: list[str] = []

    async def list_objects_all(self, prefix: str) -> list[dict]:
        return [
            {
                "key": k,
                "size": self.sizes.get(k, len(v.encode())),
                "last_modified": self.mtimes.get(k),
            }
            for k, v in self.files.items()
            if k.startswith(prefix)
        ]

    async def download_text(self, key: str) -> str:
        self.download_calls.append(key)
        return self.files[key]


# ---------------------------------------------------------------------------
# Pure extraction
# ---------------------------------------------------------------------------


def test_salvage_reads_per_trial_result_json_and_skips_job_stub():
    files = {
        # Job-level debug stub written by the runner's CancelledError handler
        # -- must be ignored (zero path segments below the prefix).
        f"{PREFIX}result.json": _result_json(n_input_tokens=999_999),
        f"{PREFIX}trial__a/result.json": _result_json(
            n_input_tokens=1000, n_cache_tokens=400, n_output_tokens=50
        ),
    }
    usage = _salvaged_usage_from_artifacts(files, PREFIX)
    assert usage.input_tokens == 1000
    assert usage.cache_tokens == 400
    assert usage.output_tokens == 50
    assert usage.cost_usd is None


def test_salvage_prefers_harbor_cost_and_reads_trajectory_extras():
    files = {
        f"{PREFIX}trial__a/result.json": _result_json(
            n_input_tokens=10, n_output_tokens=5, cost_usd=0.42
        ),
        f"{PREFIX}trial__a/agent/trajectory.json": json.dumps(
            {
                "steps": [
                    {"metrics": {"extra": {"cache_creation_input_tokens": 7}}},
                    {"metrics": {"extra": {"cache_creation_input_tokens": 3}}},
                ],
                "final_metrics": {},
            }
        ),
    }
    usage = _salvaged_usage_from_artifacts(files, PREFIX)
    assert usage.cost_usd == pytest.approx(0.42)
    assert usage.cache_write_tokens == 10
    assert usage.total_steps == 2


def test_salvage_falls_back_to_trajectory_totals():
    files = {
        f"{PREFIX}trial__a/agent/trajectory.json": json.dumps(
            {
                "steps": [{}],
                "final_metrics": {
                    "total_prompt_tokens": 111,
                    "total_completion_tokens": 22,
                    "total_cached_tokens": 33,
                },
            }
        ),
    }
    usage = _salvaged_usage_from_artifacts(files, PREFIX)
    assert usage.input_tokens == 111
    assert usage.output_tokens == 22
    assert usage.cache_tokens == 33


def test_salvage_empty_artifacts_has_no_tokens():
    assert not _salvaged_usage_from_artifacts({}, PREFIX).has_tokens()


def test_salvage_cache_only_is_not_settleable():
    # Cache tokens alone cannot be priced and would not break the unsettled
    # predicate -- has_tokens() must reject them so the row keeps retrying.
    files = {f"{PREFIX}trial__a/result.json": _result_json(n_cache_tokens=500)}
    usage = _salvaged_usage_from_artifacts(files, PREFIX)
    assert usage.cache_tokens == 500
    assert not usage.has_tokens()


def test_salvage_aggregates_step_results_when_trial_context_empty():
    # Multi-step tasks may leave the trial-level agent_result empty and only
    # populate per-step contexts (mirrors Harbor compute_token_cost_totals).
    files = {
        f"{PREFIX}trial__a/result.json": json.dumps(
            {
                "agent_result": None,
                "step_results": [
                    {"agent_result": {"n_input_tokens": 100, "n_output_tokens": 10}},
                    {"agent_result": {"n_input_tokens": 200, "n_output_tokens": 20}},
                ],
            }
        )
    }
    usage = _salvaged_usage_from_artifacts(files, PREFIX)
    assert usage.input_tokens == 300
    assert usage.output_tokens == 30


@pytest.mark.asyncio
async def test_fetch_requires_sentinel():
    from oddish.workers.queue.cleanup import _fetch_salvaged_artifacts

    # Artifacts present but no harvest-complete.json -> treat as in-flight.
    fake = _FakeStorage(
        {f"{PREFIX}trial__a/result.json": _result_json(n_input_tokens=1)}
    )
    assert await _fetch_salvaged_artifacts(fake, PREFIX) is None


@pytest.mark.asyncio
async def test_fetch_selects_newest_attempt_only():
    from datetime import datetime, timezone

    from oddish.workers.queue.cleanup import _fetch_salvaged_artifacts

    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    newer = datetime(2026, 1, 2, tzinfo=timezone.utc)
    files = _with_sentinel(
        PREFIX,
        {
            f"{PREFIX}attempt_old/result.json": _result_json(n_input_tokens=111),
            f"{PREFIX}attempt_new/result.json": _result_json(n_input_tokens=999),
        },
    )
    fake = _FakeStorage(
        files,
        mtimes={
            f"{PREFIX}attempt_old/result.json": older,
            f"{PREFIX}attempt_new/result.json": newer,
        },
    )
    got = await _fetch_salvaged_artifacts(fake, PREFIX)
    # Only the newest attempt dir is read -- summing across retries double-bills.
    assert set(got) == {f"{PREFIX}attempt_new/result.json"}
    usage = _salvaged_usage_from_artifacts(got, PREFIX)
    assert usage.input_tokens == 999


@pytest.mark.asyncio
async def test_fetch_skips_oversized_artifacts():
    from oddish.workers.queue.cleanup import (
        CANCELLED_SETTLE_MAX_ARTIFACT_BYTES,
        _fetch_salvaged_artifacts,
    )

    key = f"{PREFIX}trial__a/trajectory.json"
    files = _with_sentinel(
        PREFIX,
        {
            f"{PREFIX}trial__a/result.json": _result_json(n_input_tokens=5),
            key: "{}",
        },
    )
    fake = _FakeStorage(
        files, sizes={key: CANCELLED_SETTLE_MAX_ARTIFACT_BYTES + 1}
    )
    got = await _fetch_salvaged_artifacts(fake, PREFIX)
    assert key not in got  # oversized trajectory skipped
    assert f"{PREFIX}trial__a/result.json" in got


# ---------------------------------------------------------------------------
# DB settle pass
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settle_giveup_cache():
    """The give-up cache is a module global; isolate it per test."""
    cleanup._settle_attempts.clear()
    yield
    cleanup._settle_attempts.clear()


async def _seed_cancelled_trial(
    session, *, minutes_ago: int = 10
) -> tuple[str, str, str]:
    suffix = uuid.uuid4().hex[:6]
    org_id = f"org-stl-{suffix}"
    experiment_id = f"exp-stl-{suffix}"
    task_id = f"task-stl-{suffix}"
    trial_id = f"{task_id}-0"
    now = utcnow()
    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id=org_id,
            user="tester",
            task_path="s3://test-bucket/settle-task",
            status=TaskStatus.FAILED,
        )
    )
    session.add(
        TrialModel(
            id=trial_id,
            name=trial_id,
            task_id=task_id,
            experiment_id=experiment_id,
            org_id=org_id,
            agent="claude-code",
            provider="anthropic",
            queue_key="anthropic/claude",
            model="claude-sonnet-5",
            is_probe=False,
            max_attempts=1,
            attempts=1,
            status=TrialStatus.FAILED,
            error_message="Cancelled by user",
            harbor_stage="cancelled",
            started_at=now - timedelta(minutes=minutes_ago + 5),
            finished_at=now - timedelta(minutes=minutes_ago),
        )
    )
    await session.commit()
    return trial_id, task_id, experiment_id


async def _cleanup_rows(
    session, trial_id: str, task_id: str, experiment_id: str
) -> None:
    await session.rollback()
    await session.execute(
        TrialModel.__table__.delete().where(TrialModel.id == trial_id)
    )
    await session.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))
    await session.execute(
        ExperimentModel.__table__.delete().where(ExperimentModel.id == experiment_id)
    )
    await session.commit()


@pytest.mark.asyncio
async def test_settle_writes_usage_only_and_is_idempotent(monkeypatch, session):
    trial_id, task_id, experiment_id = await _seed_cancelled_trial(session)
    prefix = f"tasks/{task_id}/trials/{trial_id}/"
    fake = _FakeStorage(
        _with_sentinel(
            prefix,
            {
                f"{prefix}trial__a/result.json": _result_json(
                    n_input_tokens=2000,
                    n_cache_tokens=800,
                    n_output_tokens=100,
                    cost_usd=1.23,
                ),
            },
        )
    )
    import oddish.db.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_storage_client", lambda: fake)

    try:
        assert await settle_cancelled_trial_costs() == 1

        row = await session.get(TrialModel, trial_id)
        await session.refresh(row)
        assert row.input_tokens == 2000
        assert row.cache_tokens == 800
        assert row.output_tokens == 100
        assert row.cost_usd == pytest.approx(1.23)
        # Cancel semantics untouched: only usage columns were written.
        assert row.status == TrialStatus.FAILED
        assert row.error_message == "Cancelled by user"
        assert row.harbor_stage == "cancelled"

        # Settled rows exit candidacy.
        assert await settle_cancelled_trial_costs() == 0
    finally:
        await _cleanup_rows(session, trial_id, task_id, experiment_id)


@pytest.mark.asyncio
async def test_settle_estimates_cost_when_harbor_cost_missing(monkeypatch, session):
    trial_id, task_id, experiment_id = await _seed_cancelled_trial(session)
    prefix = f"tasks/{task_id}/trials/{trial_id}/"
    fake = _FakeStorage(
        _with_sentinel(
            prefix,
            {
                f"{prefix}trial__a/result.json": _result_json(
                    n_input_tokens=1000, n_cache_tokens=0, n_output_tokens=10
                ),
            },
        )
    )
    import oddish.db.storage as storage_mod
    import oddish.model_pricing as pricing_mod

    monkeypatch.setattr(storage_mod, "get_storage_client", lambda: fake)
    monkeypatch.setattr(
        pricing_mod, "estimate_cost_usd", lambda *a, **k: 0.077
    )

    try:
        assert await settle_cancelled_trial_costs() == 1
        row = await session.get(TrialModel, trial_id)
        await session.refresh(row)
        assert row.cost_usd == pytest.approx(0.077)
        assert row.input_tokens == 1000
    finally:
        await _cleanup_rows(session, trial_id, task_id, experiment_id)


@pytest.mark.asyncio
async def test_settle_leaves_row_unsettled_when_artifacts_missing(
    monkeypatch, session
):
    trial_id, task_id, experiment_id = await _seed_cancelled_trial(session)
    import oddish.db.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_storage_client", lambda: _FakeStorage({}))

    try:
        assert await settle_cancelled_trial_costs() == 0
        row = await session.get(TrialModel, trial_id)
        await session.refresh(row)
        # Stays NULL -> quota keeps billing the unpriced floor.
        assert row.cost_usd is None
        assert row.input_tokens is None
    finally:
        await _cleanup_rows(session, trial_id, task_id, experiment_id)


@pytest.mark.asyncio
async def test_settle_gives_up_on_persistently_tokenless_row(monkeypatch, session):
    # A completed harvest whose artifacts carry no countable tokens must
    # drop out of candidacy after MAX_ATTEMPTS so it can't monopolize the
    # oldest-first batch head for the whole 24h lookback.
    trial_id, task_id, experiment_id = await _seed_cancelled_trial(session)
    prefix = f"tasks/{task_id}/trials/{trial_id}/"
    fake = _FakeStorage(
        _with_sentinel(
            prefix,
            {f"{prefix}trial__a/result.json": _result_json(n_cache_tokens=42)},
        )
    )
    import oddish.db.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_storage_client", lambda: fake)

    try:
        for _ in range(cleanup.CANCELLED_SETTLE_MAX_ATTEMPTS):
            assert await settle_cancelled_trial_costs() == 0
        assert cleanup._settle_attempts.get(trial_id) == (
            cleanup.CANCELLED_SETTLE_MAX_ATTEMPTS
        )
        # Next sweep excludes the given-up row: no further S3 fetches for it.
        fake.download_calls.clear()
        assert await settle_cancelled_trial_costs() == 0
        assert fake.download_calls == []
    finally:
        await _cleanup_rows(session, trial_id, task_id, experiment_id)


@pytest.mark.asyncio
async def test_settle_write_recheck_rejects_reclaimed_running_trial(
    monkeypatch, session
):
    # A retry can re-claim the trial (harbor_stage flips off 'cancelled',
    # usage reset to NULL) between the candidate select and the write. The
    # write's re-check must reject it so the old attempt's salvage is never
    # stamped onto the live RUNNING run.
    trial_id, task_id, experiment_id = await _seed_cancelled_trial(session)
    prefix = f"tasks/{task_id}/trials/{trial_id}/"
    fake = _FakeStorage(
        _with_sentinel(
            prefix,
            {
                f"{prefix}trial__a/result.json": _result_json(
                    n_input_tokens=2000, n_output_tokens=100, cost_usd=1.23
                )
            },
        )
    )
    import oddish.db.storage as storage_mod

    monkeypatch.setattr(storage_mod, "get_storage_client", lambda: fake)

    # Simulate the retry re-claim landing after the artifacts are fetched.
    orig_fetch = cleanup._fetch_salvaged_artifacts

    async def _reclaim_then_fetch(storage, pfx):
        files = await orig_fetch(storage, pfx)
        async with cleanup.get_session() as s:
            row = await s.get(TrialModel, trial_id)
            row.harbor_stage = "starting"
            row.status = TrialStatus.RUNNING
            row.finished_at = None
        return files

    monkeypatch.setattr(cleanup, "_fetch_salvaged_artifacts", _reclaim_then_fetch)

    try:
        assert await settle_cancelled_trial_costs() == 0
        row = await session.get(TrialModel, trial_id)
        await session.refresh(row)
        # The re-claimed RUNNING row was not billed the old attempt's cost.
        assert row.cost_usd is None
        assert row.input_tokens is None
        assert row.harbor_stage == "starting"
    finally:
        await _cleanup_rows(session, trial_id, task_id, experiment_id)


# ---------------------------------------------------------------------------
# Deferred sandbox reap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reap_cancelled_sandboxes_after_grace(monkeypatch, session):
    teardowns: list[tuple] = []

    async def recording_teardown(provider, external_id):
        teardowns.append((provider, external_id))
        return True

    monkeypatch.setattr(cleanup, "cancel_job_by_worker", recording_teardown)

    suffix = uuid.uuid4().hex[:6]
    now = utcnow()
    old_job = WorkerJobModel(
        kind=WorkerJobKind.TRIAL,
        status=WorkerJobStatus.CANCELLED,
        queue_key="anthropic/claude",
        subject_table="trials",
        subject_id=f"reap-old-{suffix}",
        provider="daytona",
        external_id=f"sb-old-{suffix}",
        finished_at=now - timedelta(minutes=10),
    )
    fresh_job = WorkerJobModel(
        kind=WorkerJobKind.TRIAL,
        status=WorkerJobStatus.CANCELLED,
        queue_key="anthropic/claude",
        subject_table="trials",
        subject_id=f"reap-new-{suffix}",
        provider="daytona",
        external_id=f"sb-new-{suffix}",
        finished_at=now - timedelta(minutes=1),
    )
    session.add_all([old_job, fresh_job])
    await session.commit()
    old_id, fresh_id = old_job.id, fresh_job.id

    try:
        reaped = await reap_cancelled_trial_sandboxes()

        assert reaped >= 1
        assert ("daytona", f"sb-old-{suffix}") in teardowns
        # Inside the grace window: untouched, harvest may still be running.
        assert ("daytona", f"sb-new-{suffix}") not in teardowns

        refreshed_old = await session.get(WorkerJobModel, old_id)
        await session.refresh(refreshed_old)
        assert refreshed_old.provider is None
        assert refreshed_old.external_id is None
        refreshed_fresh = await session.get(WorkerJobModel, fresh_id)
        await session.refresh(refreshed_fresh)
        assert refreshed_fresh.external_id == f"sb-new-{suffix}"
    finally:
        await session.rollback()
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.id.in_([old_id, fresh_id])
            )
        )
        await session.commit()
