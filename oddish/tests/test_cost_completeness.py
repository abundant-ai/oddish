from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod  # noqa: E402
import oddish.trial_cost as trial_cost  # noqa: E402
from oddish.config import settings  # noqa: E402
from oddish.core import endpoints  # noqa: E402
from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
)
from oddish.trial_cost import apply_settled_cost  # noqa: E402
from oddish.workers.harbor.runner import HarborOutcome  # noqa: E402
from oddish.workers.queue import cleanup  # noqa: E402
from oddish.workers.queue import trial_handler as trial_handler_module  # noqa: E402
from oddish.workers.queue.trial_handler import _store_trial_results  # noqa: E402
import oddish.worker.local_runner as local_runner_module  # noqa: E402
from oddish.worker.local_runner import run_trial_locally  # noqa: E402

reservation_floor_usd = float(settings.pending_trial_reservation_usd)


def _billable_trial(**overrides):
    default_trial_fields = dict(
        id="trial-1",
        name="trial-1",
        task_id="task-1",
        task_version_id="task-1-v1",
        experiment_id="exp-1",
        org_id="org-1",
        billed_user_id=None,
        agent="codex",
        model="gpt-5",
        provider="openai",
        queue_key="openai/gpt-5",
        timeout_minutes=None,
        environment=None,
        is_probe=False,
        harbor_config=None,
        status=TrialStatus.RUNNING,
        error_message=None,
        harbor_stage=None,
        finished_at=None,
        started_at=None,
        max_attempts=6,
        attempts=1,
        reward=None,
        harbor_result_path=None,
        trial_s3_key=None,
        input_tokens=None,
        cache_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        total_steps=None,
        cost_usd=None,
        phase_timing=None,
        has_trajectory=False,
        current_worker_id="w-1",
        current_queue_slot=0,
        heartbeat_at=None,
        stale_reaped_at=None,
        next_retry_at=None,
        superseded_by_trial_id=None,
        deleted_at=None,
        cost_settled_attempt=0,
        result=None,
        idempotency_key="idem-1",
        claimed_at=None,
        analysis=None,
        analysis_status=None,
        analysis_error=None,
        analysis_started_at=None,
        analysis_finished_at=None,
    )
    default_trial_fields.update(overrides)
    return SimpleNamespace(**default_trial_fields)


def _outcome(reward=None, error=None, cost_usd=None, **token_fields):
    return HarborOutcome(
        reward=reward,
        error=error,
        exit_code=0,
        duration_sec=1.0,
        job_result_path=None,
        job_dir=None,
        cost_usd=cost_usd,
        **token_fields,
    )


def _patch_store_trial_results_session(monkeypatch, trial):
    @asynccontextmanager
    async def fake_trial_session(trial_id, *, allow_missing=False, with_for_update=False):
        yield object(), trial

    monkeypatch.setattr(trial_handler_module, "_trial_session", fake_trial_session)

    async def fake_maybe_start_qa(session, trial_id):
        return False

    monkeypatch.setattr("oddish.queue.maybe_start_qa_stage", fake_maybe_start_qa)


# --- apply_settled_cost: the native -> estimate -> floor chain -----------------


def test_native_cost_is_persisted_verbatim(monkeypatch):
    def estimate_must_not_be_called(*args, **kwargs):
        raise AssertionError("estimate should not run when native cost is present")

    monkeypatch.setattr(trial_cost, "estimate_cost_usd", estimate_must_not_be_called)
    trial_with_no_cost_yet = _billable_trial(cost_usd=None)

    apply_settled_cost(trial_with_no_cost_yet, _outcome(reward=1.0, cost_usd=0.12))

    assert trial_with_no_cost_yet.cost_usd == 0.12


def test_tokens_without_native_cost_persists_the_estimate(monkeypatch):
    estimated_cost_from_tokens = 0.077
    monkeypatch.setattr(
        trial_cost, "estimate_cost_usd", lambda *args, **kwargs: estimated_cost_from_tokens
    )
    trial_with_tokens_but_no_native_cost = _billable_trial(cost_usd=None)

    apply_settled_cost(
        trial_with_tokens_but_no_native_cost,
        _outcome(reward=1.0, cost_usd=None, input_tokens=1000, output_tokens=200),
    )

    assert trial_with_tokens_but_no_native_cost.cost_usd == estimated_cost_from_tokens


def test_no_native_cost_and_no_estimate_persists_the_reservation_floor(monkeypatch):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *args, **kwargs: None)
    trial_with_no_signal = _billable_trial(cost_usd=None)

    apply_settled_cost(trial_with_no_signal, _outcome(reward=1.0, cost_usd=None))

    assert trial_with_no_signal.cost_usd == reservation_floor_usd


def test_late_real_outcome_overwrites_a_prior_floor(monkeypatch):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *args, **kwargs: None)
    trial_already_floored = _billable_trial(cost_usd=reservation_floor_usd)

    apply_settled_cost(trial_already_floored, _outcome(reward=1.0, cost_usd=0.03))

    assert trial_already_floored.cost_usd == 0.03


def test_no_outcome_floors_only_when_cost_is_null(monkeypatch):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *args, **kwargs: None)

    trial_without_cost = _billable_trial(cost_usd=None)
    apply_settled_cost(trial_without_cost, None)
    assert trial_without_cost.cost_usd == reservation_floor_usd

    trial_with_real_cost = _billable_trial(cost_usd=0.07)
    apply_settled_cost(trial_with_real_cost, None)
    assert trial_with_real_cost.cost_usd == 0.07


def test_second_attempt_cost_accumulates_over_first(monkeypatch):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *a, **k: None)
    trial_on_second_attempt = _billable_trial(cost_usd=3.0, attempts=2)

    apply_settled_cost(trial_on_second_attempt, _outcome(reward=1.0, cost_usd=0.80))

    assert trial_on_second_attempt.cost_usd == pytest.approx(3.80)


def test_second_attempt_over_floored_first_adds_floor_plus_real(monkeypatch):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *a, **k: None)
    trial_floored_on_first = _billable_trial(cost_usd=reservation_floor_usd, attempts=2)

    apply_settled_cost(trial_floored_on_first, _outcome(reward=1.0, cost_usd=0.30))

    assert trial_floored_on_first.cost_usd == pytest.approx(
        reservation_floor_usd + 0.30
    )


def test_second_attempt_with_no_prior_cost_bills_only_itself(monkeypatch):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *a, **k: None)
    trial_reaped_to_retry = _billable_trial(cost_usd=None, attempts=2)

    apply_settled_cost(trial_reaped_to_retry, _outcome(reward=1.0, cost_usd=0.25))

    assert trial_reaped_to_retry.cost_usd == pytest.approx(0.25)


def test_accepted_residual_floor_plus_real_on_unsettled_prior_attempt(monkeypatch):
    """ACCEPTED over-bill residual: attempt-1 died unsettled (wrote no cost), the
    attempt-2 cancel floored, then attempt-2's late real outcome ADDS to the
    floor (attempts>1 accumulates). Bounded at one $0.50 floor on a triple-rare
    path; deliberately not special-cased."""
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *a, **k: None)
    trial = _billable_trial(cost_usd=reservation_floor_usd, attempts=2)

    apply_settled_cost(trial, _outcome(reward=None, cost_usd=0.10))

    assert trial.cost_usd == pytest.approx(reservation_floor_usd + 0.10)


def test_cancel_floor_replaced_by_late_real_outcome(monkeypatch):
    """Main regression: attempts=1 cancel floor + late real -> REPLACED, not
    added ($0.50 floor + $0.10 real must yield $0.10, never $0.60)."""
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *a, **k: None)
    trial = _billable_trial(cost_usd=reservation_floor_usd, attempts=1)

    apply_settled_cost(trial, _outcome(reward=None, cost_usd=0.10))

    assert trial.cost_usd == pytest.approx(0.10)


def test_same_attempt_outcome_redelivery_settles_once(monkeypatch):
    """At-least-once redelivery: the second delivery of the SAME attempt's
    outcome must not re-accumulate (cost_settled_attempt gates it)."""
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *a, **k: None)
    trial = _billable_trial(cost_usd=3.0, attempts=2)
    outcome = _outcome(reward=1.0, cost_usd=0.80)

    apply_settled_cost(trial, outcome)
    assert trial.cost_usd == pytest.approx(3.80)
    apply_settled_cost(trial, outcome)  # redelivery
    assert trial.cost_usd == pytest.approx(3.80)
    assert trial.cost_settled_attempt == 2


@pytest.mark.asyncio
async def test_two_attempt_costs_accumulate_across_reclaim(monkeypatch):
    """End-to-end: attempt-1 settles $3.00 and re-queues, the re-claim preserves
    cost_usd, and attempt-2's $0.80 outcome accumulates to $3.80."""
    from contextlib import asynccontextmanager

    trial = _billable_trial(cost_usd=None, attempts=1, max_attempts=6)

    class _Session:
        async def get(self, *_a, **_k):
            return None

    @asynccontextmanager
    async def fake_trial_session(trial_id, *, allow_missing=False, with_for_update=False):
        yield _Session(), trial

    monkeypatch.setattr(trial_handler_module, "_trial_session", fake_trial_session)

    async def fake_maybe_start_qa(session, trial_id):
        return False

    monkeypatch.setattr("oddish.queue.maybe_start_qa_stage", fake_maybe_start_qa)

    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=None, error="transient boom", cost_usd=3.00),
        trial_s3_key=None,
        execution_error=None,
    )
    assert trial.status == TrialStatus.RETRYING
    assert trial.cost_usd == pytest.approx(3.00)

    prepared = await trial_handler_module._prepare_trial_run(
        trial_id="trial-1",
        worker_id="w-2",
        queue_slot=1,
        modal_function_call_id=None,
    )
    assert prepared is not None
    assert trial.attempts == 2
    assert trial.cost_usd == pytest.approx(3.00)  # re-claim must NOT clear it

    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=1.0, cost_usd=0.80),
        trial_s3_key=None,
        execution_error=None,
    )
    assert trial.status == TrialStatus.SUCCESS
    assert trial.cost_usd == pytest.approx(3.80)


def test_token_fields_are_copied_from_the_outcome():
    trial_receiving_tokens = _billable_trial(cost_usd=0.01)

    apply_settled_cost(
        trial_receiving_tokens,
        _outcome(
            reward=1.0,
            cost_usd=0.01,
            input_tokens=11,
            cache_tokens=22,
            cache_write_tokens=33,
            output_tokens=44,
            total_steps=5,
        ),
    )

    assert trial_receiving_tokens.input_tokens == 11
    assert trial_receiving_tokens.cache_tokens == 22
    assert trial_receiving_tokens.cache_write_tokens == 33
    assert trial_receiving_tokens.output_tokens == 44
    assert trial_receiving_tokens.total_steps == 5


# --- S1-T1: cancel early-return persists a late outcome's partial cost ---------


@pytest.mark.asyncio
async def test_user_cancelled_trial_with_late_outcome_persists_partial_cost(monkeypatch):
    cancelled_trial = _billable_trial(
        status=TrialStatus.FAILED,
        error_message="Cancelled by user",
        finished_at="already-set-by-cancel-writer",
        max_attempts=1,
        attempts=1,
    )
    _patch_store_trial_results_session(monkeypatch, cancelled_trial)

    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(
            reward=None, cost_usd=0.04, input_tokens=500, output_tokens=90
        ),
        trial_s3_key=None,
        execution_error=None,
    )

    assert cancelled_trial.cost_usd == 0.04
    assert cancelled_trial.input_tokens == 500
    assert cancelled_trial.output_tokens == 90
    assert cancelled_trial.finished_at == "already-set-by-cancel-writer"
    assert cancelled_trial.error_message == "Cancelled by user"


@pytest.mark.asyncio
async def test_superseded_trial_with_late_outcome_persists_real_cost(monkeypatch):
    superseded_trial = _billable_trial(
        status=TrialStatus.FAILED,
        error_message="Superseded by user retry",
        finished_at="already-set-by-supersede-writer",
        superseded_by_trial_id="task-1-1",
        cost_usd=reservation_floor_usd,
        attempts=1,
    )
    _patch_store_trial_results_session(monkeypatch, superseded_trial)

    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=None, cost_usd=8.0, input_tokens=900),
        trial_s3_key=None,
        execution_error=None,
    )

    # The late outcome's real cost replaces the supersede floor; everything the
    # supersede writer stamped stays untouched.
    assert superseded_trial.cost_usd == 8.0
    assert superseded_trial.status == TrialStatus.FAILED
    assert superseded_trial.finished_at == "already-set-by-supersede-writer"
    assert superseded_trial.error_message == "Superseded by user retry"


@pytest.mark.asyncio
async def test_deleted_trial_late_outcome_replaces_floor(monkeypatch):
    """A soft-deleted trial's late real outcome must still settle -- and BEFORE
    the ownership check (deletion released ownership), so it works with
    worker_id/worker_job_id supplied (the production shape)."""
    deleted_trial = _billable_trial(
        status=TrialStatus.FAILED,
        error_message="Trial deleted by user",
        finished_at="already-set-by-delete-writer",
        deleted_at="2026-07-02",
        cost_usd=reservation_floor_usd,
        attempts=1,
    )
    _patch_store_trial_results_session(monkeypatch, deleted_trial)

    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=None, cost_usd=4.0),
        trial_s3_key=None,
        execution_error=None,
        worker_id="w-1",
        worker_job_id="wj-1",
    )

    assert deleted_trial.cost_usd == 4.0
    assert deleted_trial.status == TrialStatus.FAILED
    assert deleted_trial.finished_at == "already-set-by-delete-writer"


# --- S1-T2: a tokens-only success persists an estimate, not NULL ---------------


@pytest.mark.asyncio
async def test_success_with_tokens_but_no_native_cost_persists_an_estimate(monkeypatch):
    estimated_cost_from_tokens = 0.055
    monkeypatch.setattr(
        trial_cost, "estimate_cost_usd", lambda *args, **kwargs: estimated_cost_from_tokens
    )
    successful_trial = _billable_trial()
    _patch_store_trial_results_session(monkeypatch, successful_trial)

    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=1.0, cost_usd=None, input_tokens=1000, output_tokens=200),
        trial_s3_key=None,
        execution_error=None,
    )

    assert successful_trial.status == TrialStatus.SUCCESS
    assert successful_trial.finished_at is not None
    assert successful_trial.cost_usd == estimated_cost_from_tokens


# --- S1-T4: every settled terminal leaves cost_usd non-NULL --------------------


@pytest.mark.asyncio
async def test_successful_terminal_with_no_signal_is_floored_not_null(monkeypatch):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *args, **kwargs: None)
    successful_trial = _billable_trial()
    _patch_store_trial_results_session(monkeypatch, successful_trial)

    await _store_trial_results(
        trial_id="trial-1",
        outcome=_outcome(reward=1.0, cost_usd=None),
        trial_s3_key=None,
        execution_error=None,
    )

    assert successful_trial.finished_at is not None
    assert successful_trial.cost_usd == reservation_floor_usd


@pytest.mark.asyncio
async def test_exception_terminal_without_outcome_is_floored_not_null(monkeypatch):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *args, **kwargs: None)
    failing_trial = _billable_trial()
    _patch_store_trial_results_session(monkeypatch, failing_trial)

    await _store_trial_results(
        trial_id="trial-1",
        outcome=None,
        trial_s3_key=None,
        execution_error="worker exploded",
    )

    assert failing_trial.status == TrialStatus.FAILED
    assert failing_trial.finished_at is not None
    assert failing_trial.cost_usd == reservation_floor_usd


# --- S1-T5: the stale-worker reaper floors cost on the FAILED branch -----------


class _FakeReaperResult:
    def __init__(self, *, rows=None, mappings_rows=None, rowcount=0, scalar=None):
        self._rows = rows or []
        self._mappings_rows = mappings_rows or []
        self.rowcount = rowcount
        self._scalar = scalar

    def all(self):
        return self._mappings_rows or self._rows

    def mappings(self):
        return self

    def one_or_none(self):
        return self._mappings_rows[0] if self._mappings_rows else None

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSavepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeReaperSession:
    def __init__(self, trial):
        self.trial = trial

    def begin_nested(self):
        return _FakeSavepoint()

    async def execute(self, statement, params=None):
        sql = str(statement)
        if sql.lstrip().startswith("SELECT id FROM worker_jobs"):
            return _FakeReaperResult(rows=[("wj-1",)])
        if "UPDATE worker_jobs" in sql and "SET    status = CASE" in sql:
            return _FakeReaperResult(
                mappings_rows=[
                    {
                        "id": "wj-1",
                        "kind": "TRIAL",
                        "new_status": "FAILED",
                        "subject_table": "trials",
                        "subject_id": self.trial.id,
                        "attempts": 6,
                        "max_attempts": 6,
                        "error_message": "Worker heartbeat stalled for over 15 minutes.",
                    }
                ]
            )
        if sql.lstrip().startswith("SELECT") and "FROM trials" in sql:
            # The mirror's FOR UPDATE SKIP LOCKED trial load.
            return _FakeReaperResult(scalar=self.trial)
        return _FakeReaperResult()

    async def scalar(self, *args, **kwargs):
        return None

    async def get(self, model, object_id, **kwargs):
        from oddish.db import TrialModel

        if model is TrialModel and object_id == self.trial.id:
            return self.trial
        return None

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_stale_reaper_failed_branch_floors_cost(monkeypatch):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *args, **kwargs: None)
    stale_trial = _billable_trial(cost_usd=None, status=TrialStatus.RUNNING)
    session = _FakeReaperSession(stale_trial)

    @asynccontextmanager
    async def fake_get_session():
        yield session

    async def no_stage_transition(_session, _trial_id):
        return False

    async def fake_reap_idle_zombies():
        return 0

    monkeypatch.setattr(cleanup, "get_session", fake_get_session)
    monkeypatch.setattr(cleanup, "reap_idle_in_transaction_zombies", fake_reap_idle_zombies)
    monkeypatch.setattr("oddish.queue.maybe_start_qa_stage", no_stage_transition)
    monkeypatch.setattr(
        "oddish.queue.maybe_advance_legacy_analyzing_task", no_stage_transition
    )

    await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)

    assert stale_trial.status == TrialStatus.FAILED
    assert stale_trial.cost_usd == reservation_floor_usd


# --- S1-T6: retry-supersede floors the old trial's cost ------------------------


async def _seed_retryable_trial(
    session, *, org_id, status, cost_usd, error_message, billed_user_id=None
):
    """Insert a real org/experiment/task/trial tree for a retry test.

    retry_trial_core does session.get(TrialModel, ...) + a raw-SQL CAS UPDATE
    that a SimpleNamespace can't satisfy, so the retry-supersede floor is
    exercised against live rows. Returns (task_id, old_trial_id) and registers
    an ON DELETE CASCADE teardown that clears the committed rows.
    """
    suffix = uuid.uuid4().hex[:8]
    experiment_id = f"exp-{suffix}"
    task_id = f"task-{suffix}"
    old_trial_id = f"{task_id}-0"

    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id=org_id))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id=org_id,
            user="tester",
            task_path="s3://test-bucket/retry-fake-task",
            status=TaskStatus.RUNNING,
        )
    )
    session.add(
        TrialModel(
            id=old_trial_id,
            name=old_trial_id,
            task_id=task_id,
            experiment_id=experiment_id,
            org_id=org_id,
            billed_user_id=billed_user_id,
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5",
            model="gpt-5",
            is_probe=False,
            max_attempts=6,
            attempts=1,
            status=status,
            error_message=error_message,
            cost_usd=cost_usd,
        )
    )
    await session.flush()
    await session.commit()
    return task_id, old_trial_id, experiment_id


@pytest.mark.asyncio
async def test_retry_supersede_floors_old_trial_cost(monkeypatch, session):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *args, **kwargs: None)

    task_id, old_trial_id, experiment_id = await _seed_retryable_trial(
        session,
        org_id="org-1",
        status=TrialStatus.RUNNING,
        cost_usd=None,
        error_message="stuck",
    )

    from oddish.db import WorkerJobKind, WorkerJobModel, WorkerJobStatus

    stuck_job = WorkerJobModel(
        kind=WorkerJobKind.TRIAL,
        status=WorkerJobStatus.RUNNING,
        queue_key="openai/gpt-5",
        subject_table="trials",
        subject_id=old_trial_id,
        modal_function_call_id="fc-stuck-123",
    )
    session.add(stuck_job)
    await session.commit()

    async def fake_reserve_next_trial_index(_session, *, task_id):
        return 1

    async def fake_enqueue_trial_worker_job(_session, **kwargs):
        return None

    monkeypatch.setattr(queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index)
    monkeypatch.setattr(queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job)

    try:
        result = await endpoints.retry_trial_core(
            session, trial_id=old_trial_id, org_id="org-1"
        )
        assert result["superseded_trial_id"] == old_trial_id
        # The superseded trial's live worker job surrenders its Modal function
        # call id so the route can terminate the remote container.
        assert result["modal_function_call_ids"] == ["fc-stuck-123"]

        superseded = await session.get(TrialModel, old_trial_id)
        await session.refresh(superseded)
        assert superseded.superseded_by_trial_id == result["trial_id"]
        assert superseded.status == TrialStatus.FAILED
        assert superseded.cost_usd == reservation_floor_usd
    finally:
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id == old_trial_id
            )
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )
        await session.commit()


# --- M3: retry x deletion races must not strand an unclaimable reservation -----


@pytest.mark.asyncio
async def test_retry_races_trial_deletion_and_409s(monkeypatch, session):
    from fastapi import HTTPException
    from sqlalchemy import text as sa_text

    task_id, old_trial_id, experiment_id = await _seed_retryable_trial(
        session,
        org_id="org-1",
        status=TrialStatus.RUNNING,
        cost_usd=None,
        error_message="stuck",
    )

    async def reserve_and_race_delete(_session, *, task_id):
        # Simulate a delete winning between the retry's load and its CAS.
        await session.execute(
            sa_text("UPDATE trials SET deleted_at = NOW() WHERE id = :id"),
            {"id": old_trial_id},
        )
        return 1

    async def fail_if_enqueued(_session, **kwargs):
        raise AssertionError("must not enqueue after losing the delete race")

    monkeypatch.setattr(queue_mod, "reserve_next_trial_index", reserve_and_race_delete)
    monkeypatch.setattr(queue_mod, "enqueue_trial_worker_job", fail_if_enqueued)

    try:
        with pytest.raises(HTTPException) as raised:
            await endpoints.retry_trial_core(
                session, trial_id=old_trial_id, org_id="org-1"
            )
        assert raised.value.status_code == 409
    finally:
        await session.rollback()
        await session.execute(
            sa_text("DELETE FROM tasks WHERE id = :id"), {"id": task_id}
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_retry_races_task_deletion_and_409s(monkeypatch, session):
    from fastapi import HTTPException
    from sqlalchemy import text as sa_text

    task_id, old_trial_id, experiment_id = await _seed_retryable_trial(
        session,
        org_id="org-1",
        status=TrialStatus.RUNNING,
        cost_usd=None,
        error_message="stuck",
    )

    async def reserve_and_race_task_delete(_session, *, task_id):
        await session.execute(
            sa_text("UPDATE tasks SET deleted_at = NOW() WHERE id = :id"),
            {"id": task_id},
        )
        return 1

    async def fail_if_enqueued(_session, **kwargs):
        raise AssertionError("must not enqueue a trial under a deleted task")

    monkeypatch.setattr(
        queue_mod, "reserve_next_trial_index", reserve_and_race_task_delete
    )
    monkeypatch.setattr(queue_mod, "enqueue_trial_worker_job", fail_if_enqueued)

    try:
        with pytest.raises(HTTPException) as raised:
            await endpoints.retry_trial_core(
                session, trial_id=old_trial_id, org_id="org-1"
            )
        assert raised.value.status_code == 409
    finally:
        await session.rollback()
        await session.execute(
            sa_text("DELETE FROM tasks WHERE id = :id"), {"id": task_id}
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )
        await session.commit()


# --- deletion cancel parity: active billable trials settle before tombstone ----


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active_status",
    [TrialStatus.QUEUED, TrialStatus.RUNNING, TrialStatus.RETRYING],
)
async def test_deleting_active_billable_trial_settles_before_tombstone(
    monkeypatch, session, active_status
):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *a, **k: None)
    suffix = uuid.uuid4().hex[:6]
    org_id = f"org-del-{suffix}"
    billed_user = f"user-del-{suffix}"
    task_id, trial_id, experiment_id = await _seed_retryable_trial(
        session,
        org_id=org_id,
        status=active_status,
        cost_usd=None,
        error_message=None,
        billed_user_id=billed_user,
    )
    from oddish.db import WorkerJobKind, WorkerJobModel, WorkerJobStatus

    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.TRIAL,
            status=WorkerJobStatus.RUNNING,
            queue_key="openai/gpt-5",
            subject_table="trials",
            subject_id=trial_id,
            modal_function_call_id="fc-del-1",
        )
    )
    await session.commit()

    try:
        result = await endpoints.delete_trial_core(
            session, trial_id=trial_id, org_id=org_id
        )
        assert result["modal_function_call_ids"] == ["fc-del-1"]

        from sqlalchemy import text as sa_text

        from oddish.core.quotas import inflight_count, start_of_today_utc, sum_cost_usd

        row = (
            await session.execute(
                sa_text(
                    "SELECT status::text AS status, finished_at, cost_usd, deleted_at "
                    "FROM trials WHERE id = :id"
                ),
                {"id": trial_id},
            )
        ).one()
        assert row.status == "FAILED"
        assert row.finished_at is not None
        assert row.cost_usd == reservation_floor_usd
        assert row.deleted_at is not None

        # Settled spend keeps counting; the reservation is gone.
        settled = await sum_cost_usd(session, org_id, billed_user, start_of_today_utc())
        assert float(settled) == pytest.approx(reservation_floor_usd)
        assert await inflight_count(session, org_id, billed_user) == 0
    finally:
        await session.rollback()
        from oddish.db import WorkerJobModel as _WJ

        await session.execute(
            _WJ.__table__.delete().where(_WJ.subject_id == trial_id)
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_deleting_experiment_settles_all_active_billable_trials(
    monkeypatch, session
):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *a, **k: None)
    suffix = uuid.uuid4().hex[:6]
    org_id = f"org-delx-{suffix}"
    billed_user = f"user-delx-{suffix}"
    task_id, first_trial_id, experiment_id = await _seed_retryable_trial(
        session,
        org_id=org_id,
        status=TrialStatus.RUNNING,
        cost_usd=None,
        error_message=None,
        billed_user_id=billed_user,
    )
    second_trial_id = f"{task_id}-1"
    session.add(
        TrialModel(
            id=second_trial_id,
            name=second_trial_id,
            task_id=task_id,
            experiment_id=experiment_id,
            org_id=org_id,
            billed_user_id=billed_user,
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5",
            model="gpt-5",
            is_probe=False,
            max_attempts=6,
            attempts=0,
            status=TrialStatus.QUEUED,
        )
    )
    await session.commit()

    try:
        await endpoints.delete_experiment_core(
            session, experiment_id=experiment_id, org_id=org_id
        )

        from sqlalchemy import text as sa_text

        from oddish.core.quotas import inflight_count, start_of_today_utc, sum_cost_usd

        rows = (
            await session.execute(
                sa_text(
                    "SELECT status::text AS status, finished_at, cost_usd, deleted_at "
                    "FROM trials WHERE task_id = :task_id ORDER BY id"
                ),
                {"task_id": task_id},
            )
        ).all()
        assert len(rows) == 2
        for row in rows:
            assert row.status == "FAILED"
            assert row.finished_at is not None
            assert row.cost_usd == reservation_floor_usd
            assert row.deleted_at is not None

        settled = await sum_cost_usd(session, org_id, billed_user, start_of_today_utc())
        assert float(settled) == pytest.approx(2 * reservation_floor_usd)
        assert await inflight_count(session, org_id, billed_user) == 0
    finally:
        await session.rollback()
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )
        await session.commit()


# --- reaper skip-locked: never wait on a trial row while holding worker_jobs ---


@pytest.mark.asyncio
async def test_reaper_skips_trial_row_locked_by_another_session(monkeypatch, session):
    import oddish.db.connection as _conn
    from sqlalchemy import select as sa_select
    from sqlalchemy import text as sa_text

    from oddish.db import WorkerJobKind, WorkerJobModel, WorkerJobStatus, utcnow

    await session.execute(
        sa_text(
            "CREATE TABLE IF NOT EXISTS tag_projection_sweep_state ("
            "id BOOLEAN PRIMARY KEY DEFAULT TRUE, "
            "last_full_sweep_at TIMESTAMPTZ, "
            "CONSTRAINT tag_sweep_singleton CHECK (id))"
        )
    )
    await session.commit()

    suffix = uuid.uuid4().hex[:6]
    org_id = f"org-reap-{suffix}"
    task_id, trial_id, experiment_id = await _seed_retryable_trial(
        session,
        org_id=org_id,
        status=TrialStatus.RUNNING,
        cost_usd=None,
        error_message=None,
    )
    from datetime import timedelta

    session.add(
        WorkerJobModel(
            kind=WorkerJobKind.TRIAL,
            status=WorkerJobStatus.RUNNING,
            queue_key="openai/gpt-5",
            subject_table="trials",
            subject_id=trial_id,
            attempts=6,
            max_attempts=6,
            heartbeat_at=utcnow() - timedelta(hours=1),
        )
    )
    await session.commit()

    async def no_zombie_reap():
        return 0

    monkeypatch.setattr(cleanup, "reap_idle_in_transaction_zombies", no_zombie_reap)

    try:
        async with _conn.async_session_maker() as locker:
            await locker.execute(
                sa_select(TrialModel)
                .where(TrialModel.id == trial_id)
                .with_for_update()
            )
            # The reaper CASes the stale worker_jobs row, then must SKIP the
            # locked trial row instead of blocking on it (deadlock risk).
            await cleanup.cleanup_orphaned_queue_state(stale_after_minutes=15)
            await locker.rollback()

        untouched = await session.get(TrialModel, trial_id)
        await session.refresh(untouched)
        assert untouched.status == TrialStatus.RUNNING
        assert untouched.cost_usd is None
        assert untouched.stale_reaped_at is None

        # R2: the job's CAS rolled back with the skipped mirror (per-job
        # savepoint), so the whole unit is retried next sweep -- no terminal
        # job stranded next to a live trial.
        from sqlalchemy import text as sa_text

        job_status = await session.scalar(
            sa_text(
                "SELECT status::text FROM worker_jobs WHERE subject_id = :tid"
            ),
            {"tid": trial_id},
        )
        assert job_status == "RUNNING"
    finally:
        await session.rollback()
        await session.execute(
            WorkerJobModel.__table__.delete().where(
                WorkerJobModel.subject_id == trial_id
            )
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )
        await session.commit()


# --- S1-review: the estimate path must never raise in a settlement path --------


def test_non_int_token_never_raises_and_falls_back_to_floor():
    trial_with_non_int_tokens = _billable_trial(cost_usd=None)

    apply_settled_cost(
        trial_with_non_int_tokens,
        _outcome(reward=1.0, cost_usd=None, input_tokens="1000.0", output_tokens="200"),
    )

    assert trial_with_non_int_tokens.cost_usd == reservation_floor_usd


# --- S1-review: the local-mode runner floors cost on its terminals -------------


class _LocalRunnerSession:
    def __init__(self, trial):
        self._trial = trial

    async def get(self, model, trial_id):
        return self._trial


def _patch_local_runner(monkeypatch, trial, harbor_run):
    session = _LocalRunnerSession(trial)

    @asynccontextmanager
    async def fake_get_session():
        yield session

    monkeypatch.setattr(local_runner_module, "get_session", fake_get_session)
    monkeypatch.setattr(local_runner_module, "_run_harbor_trial", harbor_run)


@pytest.mark.asyncio
async def test_local_runner_success_without_cost_is_floored(monkeypatch):
    local_trial = _billable_trial(cost_usd=None)

    async def harbor_run_writes_no_cost(trial_id):
        return None

    _patch_local_runner(monkeypatch, local_trial, harbor_run_writes_no_cost)

    await run_trial_locally("trial-1", dry_run=False)

    assert local_trial.status == TrialStatus.SUCCESS
    assert local_trial.cost_usd == reservation_floor_usd


@pytest.mark.asyncio
async def test_local_runner_failure_is_floored(monkeypatch):
    local_trial = _billable_trial(cost_usd=None)

    async def harbor_run_raises(trial_id):
        raise RuntimeError("local harbor blew up")

    _patch_local_runner(monkeypatch, local_trial, harbor_run_raises)

    with pytest.raises(RuntimeError):
        await run_trial_locally("trial-1", dry_run=False)

    assert local_trial.status == TrialStatus.FAILED
    assert local_trial.cost_usd == reservation_floor_usd


@pytest.mark.asyncio
async def test_local_runner_dry_run_is_not_charged(monkeypatch):
    local_trial = _billable_trial(cost_usd=None)

    async def harbor_run_must_not_be_called(trial_id):
        raise AssertionError("dry_run must not run harbor")

    _patch_local_runner(monkeypatch, local_trial, harbor_run_must_not_be_called)

    await run_trial_locally("trial-1", dry_run=True)

    assert local_trial.status == TrialStatus.SUCCESS
    assert local_trial.cost_usd is None
