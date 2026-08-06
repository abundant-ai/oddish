from __future__ import annotations

import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import oddish.queue as queue_mod  # noqa: E402
from oddish.core import endpoints  # noqa: E402
from oddish.db import (  # noqa: E402
    ExperimentModel,
    TaskModel,
    TaskStatus,
    TrialModel,
    TrialStatus,
)
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
    matching_migrations = list(
        versions_dir.glob("billed_user_*add_trials_billed_user_id.py")
    )
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
    """Spy for ``_bulk_insert_trials``, which issues two statements: the
    trials bulk insert, then the trial_facets vocabulary upsert. The
    billed_user_id assertion targets the first."""

    def __init__(self):
        self.captured_calls = []

    async def execute(self, statement, params=None):
        self.captured_calls.append(params)

    @property
    def captured_params(self):
        return self.captured_calls[0]


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
    # Second statement is the write-through facet-vocabulary upsert.
    assert len(capturing_session.captured_calls) == 2


def test_bulk_insert_sql_keeps_billed_user_id_arity_aligned():
    sql_text = str(_TRIAL_BULK_INSERT_SQL)
    assert sql_text.count("billed_user_id") >= 3


# --- S2-T5: retry carries the payer forward (NULL stays NULL) ------------------


async def _run_retry(monkeypatch, session, *, billed_user_id):
    """Seed a real trial, drive retry_trial_core, return the committed new trial.

    retry_trial_core reads the source row via session.get(TrialModel, ...) and
    supersedes it with a raw-SQL CAS UPDATE, so a SimpleNamespace can't stand in
    for the session. Seed live rows, run the real function, and re-read the
    inserted replacement trial to assert its billed_user_id was carried forward.
    """
    suffix = uuid.uuid4().hex[:8]
    experiment_id = f"exp-{suffix}"
    task_id = f"task-{suffix}"
    old_trial_id = f"{task_id}-0"

    session.add(ExperimentModel(id=experiment_id, name=experiment_id, org_id="org-1"))
    session.add(
        TaskModel(
            id=task_id,
            name=task_id,
            org_id="org-1",
            user="tester",
            task_path="s3://test-bucket/retry-fake-task",
            status=TaskStatus.COMPLETED,
        )
    )
    session.add(
        TrialModel(
            id=old_trial_id,
            name=old_trial_id,
            task_id=task_id,
            experiment_id=experiment_id,
            org_id="org-1",
            agent="codex",
            provider="openai",
            queue_key="openai/gpt-5",
            model="gpt-5",
            is_probe=False,
            max_attempts=6,
            attempts=1,
            status=TrialStatus.FAILED,
            error_message="boom",
            billed_user_id=billed_user_id,
            cost_usd=0.01,
        )
    )
    await session.flush()
    await session.commit()

    async def fake_reserve_next_trial_index(_session, *, task_id):
        return 1

    async def fake_enqueue_trial_worker_job(_session, **kwargs):
        return None

    monkeypatch.setattr(
        queue_mod, "reserve_next_trial_index", fake_reserve_next_trial_index
    )
    monkeypatch.setattr(
        queue_mod, "enqueue_trial_worker_job", fake_enqueue_trial_worker_job
    )

    try:
        result = await endpoints.retry_trial_core(
            session, trial_id=old_trial_id, org_id="org-1"
        )
        new_trial = await session.get(TrialModel, result["trial_id"])
        await session.refresh(new_trial)
        return new_trial.billed_user_id
    finally:
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(
                ExperimentModel.id == experiment_id
            )
        )
        await session.commit()


# --- S2 review: auto-probe forwards the payer to the probe trial insert --------


class _PresetSession:
    async def scalar(self, *args, **kwargs):
        return SimpleNamespace(
            id="preset-1",
            agent="nop",
            name="probe",
            operator_prompt="probe the task",
            result_focus="focus",
            evaluation_metric="metric",
        )


@pytest.mark.asyncio
async def test_auto_probe_forwards_billed_user_id_to_append(monkeypatch):
    import oddish.core.probe.auto_probe as auto_probe_module

    captured_append_kwargs = {}

    async def fake_version_already_probed(session, version_id):
        return False

    async def fake_probed_version_count(session, org_id):
        return 0

    async def fake_append_trials_to_task(
        session, *, task, submission, experiment_id=None, billed_user_id=None
    ):
        captured_append_kwargs["billed_user_id"] = billed_user_id
        return []

    monkeypatch.setattr(
        auto_probe_module, "_version_already_probed", fake_version_already_probed
    )
    monkeypatch.setattr(
        auto_probe_module, "_probed_version_count", fake_probed_version_count
    )
    monkeypatch.setattr(auto_probe_module, "next_probe_model", lambda count: "gpt-5")
    monkeypatch.setattr(
        auto_probe_module, "build_trial_specs_from_sweep", lambda submission: []
    )
    monkeypatch.setattr(
        auto_probe_module,
        "build_task_submission_from_sweep",
        lambda submission, task_path, trials: SimpleNamespace(trials=trials),
    )
    monkeypatch.setattr(
        auto_probe_module, "append_trials_to_task", fake_append_trials_to_task
    )

    probed_task = SimpleNamespace(
        current_version_id="task-1-v1",
        id="task-1",
        name="task-1",
        task_path="/tmp/task-1",
        user=None,
    )

    await auto_probe_module.maybe_enqueue_auto_probe(
        _PresetSession(),
        task=probed_task,
        experiment=None,
        org_id="org-1",
        billed_user_id="user-9",
    )

    assert captured_append_kwargs["billed_user_id"] == "user-9"


@pytest.mark.asyncio
async def test_retry_carries_billed_user_id_forward(monkeypatch, session):
    replacement_billed_user_id = await _run_retry(
        monkeypatch, session, billed_user_id="user-42"
    )
    assert replacement_billed_user_id == "user-42"


@pytest.mark.asyncio
async def test_retry_of_null_billed_trial_stays_null(monkeypatch, session):
    replacement_billed_user_id = await _run_retry(
        monkeypatch, session, billed_user_id=None
    )
    assert replacement_billed_user_id is None
