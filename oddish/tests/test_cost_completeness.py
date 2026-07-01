from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod  # noqa: E402
import oddish.trial_cost as trial_cost  # noqa: E402
from oddish.config import settings  # noqa: E402
from oddish.core import endpoints  # noqa: E402
from oddish.db import TaskStatus, TrialStatus  # noqa: E402
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
    async def fake_trial_session(trial_id, *, allow_missing=False):
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
    def __init__(self, *, mappings_rows=None, rowcount=0):
        self._mappings_rows = mappings_rows or []
        self.rowcount = rowcount

    def all(self):
        return self._mappings_rows

    def mappings(self):
        return self


class _FakeReaperSession:
    def __init__(self, trial):
        self.trial = trial

    async def execute(self, statement, params=None):
        sql = str(statement)
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
        return _FakeReaperResult()

    async def scalar(self, *args, **kwargs):
        return None

    async def get(self, model, object_id):
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


class _RetryResult:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _RetrySession:
    def __init__(self, *, old_trial, task):
        self.old_trial = old_trial
        self.task = task
        self.added = []

    async def execute(self, _statement, _params=None):
        return _RetryResult(scalar=self.old_trial)

    async def get(self, _model, _key):
        return self.task

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None


@pytest.mark.asyncio
async def test_retry_supersede_floors_old_trial_cost(monkeypatch):
    monkeypatch.setattr(trial_cost, "estimate_cost_usd", lambda *args, **kwargs: None)
    stuck_old_trial = _billable_trial(
        id="task-1-0",
        name="task-1-0",
        status=TrialStatus.RUNNING,
        error_message="stuck",
        cost_usd=None,
    )
    task = SimpleNamespace(
        id="task-1", name="task-1", status=TaskStatus.RUNNING, finished_at=None
    )
    session = _RetrySession(old_trial=stuck_old_trial, task=task)

    async def fake_reserve_next_trial_index(_session, *, task_id):
        return 1

    async def fake_enqueue_trial_worker_job(_session, **kwargs):
        return None

    monkeypatch.setattr(queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index)
    monkeypatch.setattr(queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job)

    await endpoints.retry_trial_core(session, trial_id="task-1-0", org_id="org-1")

    assert stuck_old_trial.superseded_by_trial_id == "task-1-1"
    assert stuck_old_trial.status == TrialStatus.FAILED
    assert stuck_old_trial.cost_usd == reservation_floor_usd


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
