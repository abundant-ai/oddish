from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod  # noqa: E402
from oddish.core import endpoints  # noqa: E402
from oddish.db import TaskStatus, TrialModel, TrialStatus  # noqa: E402
from oddish.queue import _TRIAL_BULK_INSERT_SQL, _bulk_insert_trials  # noqa: E402


# --- S2-T1: the column is a nullable, unconstrained String(64) -----------------


def test_billed_user_id_is_nullable_unconstrained_string64():
    column = TrialModel.__table__.columns["billed_user_id"]
    assert column.nullable is True
    assert column.foreign_keys == set()
    assert column.type.length == 64


def test_partial_billed_user_spend_index_exists():
    index_names = {index.name for index in TrialModel.__table__.indexes}
    assert "idx_trials_org_billed_user_finished" in index_names


# --- S2-T2: the migration is DDL-only and builds the index CONCURRENTLY --------


def test_billed_user_migration_is_ddl_only_with_concurrent_index():
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    matching_migrations = list(versions_dir.glob("billed_user_*add_trials_billed_user_id.py"))
    assert len(matching_migrations) == 1

    migration_source = matching_migrations[0].read_text()
    assert "billed_user_id" in migration_source
    assert "CONCURRENTLY" in migration_source
    assert "autocommit_block" in migration_source

    uppercased_source = migration_source.upper()
    assert "INSERT" not in uppercased_source
    assert "UPDATE" not in uppercased_source


# --- S2-T3: combine and import never attribute a payer -------------------------


def test_combine_result_fields_exclude_billed_user_id():
    from oddish.core.endpoints.deletion import _COMBINE_TRIAL_RESULT_FIELDS

    assert "billed_user_id" not in _COMBINE_TRIAL_RESULT_FIELDS


def test_new_trial_defaults_billed_user_id_to_null():
    freshly_constructed_trial = TrialModel(id="t1", task_id="task-1", agent="codex")
    assert freshly_constructed_trial.billed_user_id is None


# --- S2-T4: the sweep bulk-insert threads billed_user_id through the raw SQL ----


def _bulk_insert_trial_row(**overrides):
    row = dict(
        id="task-1-0",
        name="task-1-0",
        task_id="task-1",
        task_version_id="task-1-v1",
        experiment_id="exp-1",
        org_id="org-1",
        agent="codex",
        provider="openai",
        queue_key="openai/gpt-5",
        model="gpt-5",
        timeout_minutes=None,
        environment=None,
        harbor_config=None,
        harbor_sha="sha-1",
        is_probe=False,
        max_attempts=6,
        billed_user_id="user-42",
    )
    row.update(overrides)
    return row


class _CapturingSession:
    def __init__(self):
        self.captured_params = None

    async def execute(self, statement, params=None):
        self.captured_params = params


@pytest.mark.asyncio
async def test_bulk_insert_threads_billed_user_id_into_params():
    capturing_session = _CapturingSession()

    await _bulk_insert_trials(
        capturing_session,
        [
            _bulk_insert_trial_row(id="task-1-0", billed_user_id="user-42"),
            _bulk_insert_trial_row(id="task-1-1", billed_user_id=None),
        ],
    )

    assert capturing_session.captured_params["billed_user_id"] == ["user-42", None]


def test_bulk_insert_sql_keeps_billed_user_id_arity_aligned():
    sql_text = str(_TRIAL_BULK_INSERT_SQL)
    assert sql_text.count("billed_user_id") >= 3


# --- S2-T5: retry carries the payer forward (NULL stays NULL) ------------------


class _RetryResult:
    def __init__(self, scalar):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _RetrySession:
    def __init__(self, *, old_trial, task):
        self.old_trial = old_trial
        self.task = task
        self.added = []

    async def execute(self, _statement, _params=None):
        return _RetryResult(self.old_trial)

    async def get(self, _model, _key):
        return self.task

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        return None


def _retryable_old_trial(billed_user_id):
    return SimpleNamespace(
        id="task-1-0",
        name="task-1-0",
        task_id="task-1",
        task_version_id="task-1-v1",
        experiment_id="exp-1",
        org_id="org-1",
        agent="codex",
        model="gpt-5",
        provider="openai",
        queue_key="openai/gpt-5",
        timeout_minutes=None,
        environment=None,
        harbor_config=None,
        is_probe=False,
        max_attempts=6,
        status=TrialStatus.FAILED,
        error_message="boom",
        harbor_stage=None,
        finished_at=None,
        current_worker_id=None,
        current_queue_slot=None,
        superseded_by_trial_id=None,
        billed_user_id=billed_user_id,
        input_tokens=None,
        cache_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        total_steps=None,
        cost_usd=0.01,
    )


async def _run_retry(monkeypatch, old_trial):
    task = SimpleNamespace(
        id="task-1", name="task-1", status=TaskStatus.COMPLETED, finished_at=None
    )
    session = _RetrySession(old_trial=old_trial, task=task)

    async def fake_reserve_next_trial_index(_session, *, task_id):
        return 1

    async def fake_enqueue_trial_worker_job(_session, **kwargs):
        return None

    monkeypatch.setattr(queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index)
    monkeypatch.setattr(queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job)

    await endpoints.retry_trial_core(session, trial_id="task-1-0", org_id="org-1")
    return session.added[0]


@pytest.mark.asyncio
async def test_retry_carries_billed_user_id_forward(monkeypatch):
    replacement_trial = await _run_retry(monkeypatch, _retryable_old_trial("user-42"))
    assert replacement_trial.billed_user_id == "user-42"


@pytest.mark.asyncio
async def test_retry_of_null_billed_trial_stays_null(monkeypatch):
    replacement_trial = await _run_retry(monkeypatch, _retryable_old_trial(None))
    assert replacement_trial.billed_user_id is None
