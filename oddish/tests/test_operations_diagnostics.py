"""Exercise restored diagnostic bodies locally without Modal calls or cloud storage."""

import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

import oddish.db as db

modal = pytest.importorskip(
    "modal", reason="Modal diagnostics require the worker extra"
)


@pytest.fixture
def load_diagnostic(monkeypatch):
    # Real Modal decorators, but no deployed image or runtime secrets are needed
    # when calling the function body via .local().
    monkeypatch.setitem(
        sys.modules,
        "modal_app",
        SimpleNamespace(image=modal.Image.debian_slim(), runtime_secrets=[]),
    )

    def load(path):
        source = Path(__file__).resolve().parents[2] / "backend" / path
        spec = importlib.util.spec_from_file_location(source.stem, source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


@pytest.mark.asyncio
async def test_migration_report_continues_after_missing_table(
    load_diagnostic, monkeypatch, session
):
    diagnostic = load_diagnostic("check_alembic_current_modal.py")
    await session.execute(
        text("CREATE TEMP TABLE restored_revision_test (version_num text)")
    )
    await session.execute(
        text("INSERT INTO restored_revision_test VALUES ('revision-2')")
    )
    monkeypatch.setattr(
        diagnostic,
        "_VERSION_TABLES",
        {
            "missing": "restored_missing_revision_test",
            "present": "restored_revision_test",
        },
    )

    @asynccontextmanager
    async def same_session():
        yield session

    monkeypatch.setattr(db, "get_session", same_session)
    result = await diagnostic.current.local()
    assert "UndefinedTableError" in result["missing"][0]
    assert result["present"] == ["revision-2"]


@pytest.mark.asyncio
async def test_unscored_trial_preserves_storage_read_error(
    load_diagnostic, monkeypatch
):
    diagnostic = load_diagnostic("probe_docker_failures_modal.py")
    trial = db.TrialModel(id="trial-1", task_id="task-1", status=db.TrialStatus.RUNNING)
    results = iter([[trial], []])

    async def execute(query):
        rows = next(results)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(db, "get_session", session)
    storage = SimpleNamespace(
        _trial_prefix=lambda trial_id: trial_id,
        get_trial_result_json=AsyncMock(side_effect=OSError("storage unavailable")),
    )
    monkeypatch.setattr(db, "get_storage_client", lambda: storage)
    result = json.loads(await diagnostic.examine.local(72, True, 10, "Docker"))
    assert (
        result["trials"][0]["result_json_read_error"] == "OSError: storage unavailable"
    )
    assert result["trials"][0]["result_json_exception_type"] is None
    assert "unscored trials" in result["aggregate"]["note"]
    assert "failed trials" not in result["aggregate"]["note"]


@pytest.mark.asyncio
async def test_audit_read_failure_is_not_reported_as_missing_artifact(
    load_diagnostic, monkeypatch
):
    import oddish.db.storage

    diagnostic = load_diagnostic("probe_audit_trail_modal.py")
    trial = db.TrialModel(id="trial-1", task_id="task-1", reward=1)
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [])
            )
        )
    )
    storage = SimpleNamespace(
        _trial_prefix=lambda trial_id: trial_id,
        list_keys=AsyncMock(side_effect=OSError("storage unavailable")),
        get_trial_result_json=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        oddish.db.storage,
        "resolve_trial_directory",
        AsyncMock(side_effect=OSError("download unavailable")),
    )
    result = await diagnostic._trail_for_trial(session, storage, trial)
    assert result["verdict"].startswith("READ ERROR:")
    assert "storage unavailable" in result["artifacts"]["list_error"]


@pytest.mark.asyncio
async def test_tail_report_measures_requested_cohort(load_diagnostic, monkeypatch):
    import oddish.core.trial_io

    diagnostic = load_diagnostic("scripts/tail_budget_report.py")
    trials = [
        SimpleNamespace(analysis={"classification": label})
        for label in ["GOOD_FAILURE", "GOOD_FAILURE", "BAD_FAILURE"]
    ]

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(
                    all=lambda: [(trial, "unused-path") for trial in trials]
                )
            )
        )

    monkeypatch.setattr(db, "get_session", session)
    read = AsyncMock(side_effect=[{"text": "x" * 3000}, None])
    monkeypatch.setattr(oddish.core.trial_io, "read_trial_trajectory", read)
    result = json.loads(
        await diagnostic.report.local("experiment-1", "GOOD_FAILURE", 100)
    )
    assert read.await_count == 2
    assert result["trials_measured"] == 1
    assert result["trajectories_unreadable"] == 1
    assert result["trajectory_bytes"]["total"] == len(
        json.dumps({"text": "x" * 3000}).encode()
    )
    assert result["budgets"][0]["cohort_bytes_at_this_budget"] == 2000
    assert result["budgets"][0]["trials_fully_shown"] == "0/1"
    assert result["budgets"][1]["trials_fully_shown"] == "1/1"
