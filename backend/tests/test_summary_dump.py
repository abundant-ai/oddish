"""Tests for api.services.summary_dump."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import models  # noqa: F401  registers cloud tables on the shared Base
from api.services.summarize_trajectory import TaskContext
from api.services.summary_dump import resolve_cohort, validate_scope
from models import OrganizationModel
from oddish.db.models import (
    Base,
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    experiment_trials,
)


def test_validate_scope_rejects_no_scope():
    with pytest.raises(ValueError, match="exactly one"):
        validate_scope(trials=None, task=None, experiment=None)


def test_validate_scope_rejects_two_scopes():
    with pytest.raises(ValueError, match="exactly one"):
        validate_scope(trials=["tr_a"], task="my-task", experiment=None)


def test_validate_scope_accepts_single_scope():
    validate_scope(trials=["tr_a"], task=None, experiment=None)
    validate_scope(trials=None, task="my-task", experiment=None)
    validate_scope(trials=None, task=None, experiment="exp_1")


def test_validate_scope_rejects_empty_trial_list():
    with pytest.raises(ValueError, match="exactly one"):
        validate_scope(trials=[], task=None, experiment=None)


def _candidate(
    trial_id: str, *, has_trajectory=True, agent="claude-code", finished_at=object()
):
    return SimpleNamespace(
        id=trial_id,
        has_trajectory=has_trajectory,
        agent=agent,
        finished_at=finished_at,
    )


def test_filter_fetchable_keeps_only_trials_with_a_trajectory():
    from api.services.summary_dump import filter_fetchable

    rows = [
        _candidate("tr_a"),
        _candidate("tr_b", has_trajectory=False),
        _candidate("tr_c", has_trajectory=False, agent="grok-build"),
        _candidate("tr_d", has_trajectory=False, agent="grok-build", finished_at=None),
    ]
    assert [t.id for t in filter_fetchable(rows)] == ["tr_a", "tr_c"]


def test_filter_fetchable_applies_limit_after_filtering():
    from api.services.summary_dump import filter_fetchable

    rows = [
        _candidate("tr_a", has_trajectory=False),
        _candidate("tr_b"),
        _candidate("tr_c"),
    ]
    assert [t.id for t in filter_fetchable(rows, limit=1)] == ["tr_b"]


# ---------------------------------------------------------------------------
# resolve_cohort -- DB-backed tests
#
# Same schema-reset pattern as test_task_affiliated_experiments.py: wipe and
# rebuild the public schema per test against ODDISH_DATABASE_URL, so tests
# don't need unique-suffixed ids and can't see each other's rows.
# ---------------------------------------------------------------------------

URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not URL, reason="ODDISH_DATABASE_URL not set")


@pytest.fixture(autouse=True)
def _stub_prompt_registry(monkeypatch):
    """summarize_trial fetches the registry template on every call (its own
    session, not the caller's) -- stub it so these tests don't need a live
    Postgres just to exercise the summary path."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(
        "api.services.summarize_trajectory._load_summary_prompt",
        AsyncMock(return_value=("TEMPLATE", 1, "prompt-test-id")),
    )


@asynccontextmanager
async def _fresh_db():
    engine = create_async_engine(URL)
    try:
        async with engine.begin() as c:
            await c.execute(text("drop schema public cascade"))
            await c.execute(text("create schema public"))
            await c.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


def _trial(id_, *, task_id, experiment_id, org_id="org1", **overrides):
    defaults = dict(
        name=f"{id_}-name",
        agent="claude-code",
        provider="anthropic",
        model="anthropic/claude-sonnet-4-6",
        queue_key="test-summary-dump",
        status=TrialStatus.SUCCESS,
        origin=TrialOrigin.ODDISH,
        is_probe=False,
        has_trajectory=True,
    )
    defaults.update(overrides)
    return TrialModel(
        id=id_, task_id=task_id, experiment_id=experiment_id, org_id=org_id, **defaults
    )


async def _seed_org_experiment_task(session, *, task_id="task1", task_name="task-1"):
    session.add(OrganizationModel(id="org1", name="Org", slug="org"))
    session.add(ExperimentModel(id="exp1", name="Exp", org_id="org1"))
    session.add(
        TaskModel(id=task_id, name=task_name, user="u", task_path="/p", org_id="org1")
    )
    await session.flush()


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_experiment_scope_excludes_probes():
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            session.add(
                _trial("tr-real", task_id="task1", experiment_id="exp1", is_probe=False)
            )
            session.add(
                _trial("tr-probe", task_id="task1", experiment_id="exp1", is_probe=True)
            )
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, experiment="exp1")

    assert [t.id for t in result] == ["tr-real"]


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_task_scope_resolves_by_name_via_join():
    async with _fresh_db() as maker:
        async with maker() as session:
            session.add(OrganizationModel(id="org1", name="Org", slug="org"))
            session.add(ExperimentModel(id="exp1", name="Exp", org_id="org1"))
            session.add(
                TaskModel(
                    id="task-x",
                    name="task-x-name",
                    user="u",
                    task_path="/p",
                    org_id="org1",
                )
            )
            session.add(
                TaskModel(
                    id="task-y",
                    name="task-y-name",
                    user="u",
                    task_path="/p",
                    org_id="org1",
                )
            )
            await session.flush()
            session.add(_trial("tr-x", task_id="task-x", experiment_id="exp1"))
            session.add(_trial("tr-y", task_id="task-y", experiment_id="exp1"))
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, task="task-x-name")

    assert [t.id for t in result] == ["tr-x"]
    # Task 3 reads trials after the session (and engine) is gone; `.task` must
    # already be populated via the mapper-level selectin eager load, not lazy.
    assert result[0].task.name == "task-x-name"


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_orders_by_trial_id_ascending():
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            for tid in ("trial-c", "trial-a", "trial-b"):  # insert out of id order
                session.add(_trial(tid, task_id="task1", experiment_id="exp1"))
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, experiment="exp1")

    assert [t.id for t in result] == ["trial-a", "trial-b", "trial-c"]


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_explicit_trials_preserve_order_and_drop_missing():
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            session.add(_trial("trial-x", task_id="task1", experiment_id="exp1"))
            session.add(_trial("trial-y", task_id="task1", experiment_id="exp1"))
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(
                session, trials=["trial-y", "trial-missing", "trial-x"]
            )

    assert [t.id for t in result] == ["trial-y", "trial-x"]


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_limit_applies_after_fetchable_filter():
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            # "trial-1-bad" sorts first by id but isn't fetchable; a SQL LIMIT
            # applied before filtering would return zero fetchable rows here.
            session.add(
                _trial(
                    "trial-1-bad",
                    task_id="task1",
                    experiment_id="exp1",
                    has_trajectory=False,
                )
            )
            session.add(
                _trial(
                    "trial-2-good",
                    task_id="task1",
                    experiment_id="exp1",
                    has_trajectory=True,
                )
            )
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, experiment="exp1", limit=1)

    assert [t.id for t in result] == ["trial-2-good"]


async def _link_collection(session, *, experiment_id, trial_id, deleted_at=None):
    await session.execute(
        experiment_trials.insert().values(
            experiment_id=experiment_id, trial_id=trial_id, deleted_at=deleted_at
        )
    )


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_experiment_scope_includes_collection_linked_trial():
    """Regression test for the reported bug: a collection experiment gathers
    an existing trial via `experiment_trials` WITHOUT moving it, so the
    trial's home `experiment_id` still points elsewhere."""
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            session.add(
                ExperimentModel(id="exp2-collection", name="Collection", org_id="org1")
            )
            session.add(_trial("tr-collected", task_id="task1", experiment_id="exp1"))
            await session.flush()
            await _link_collection(
                session, experiment_id="exp2-collection", trial_id="tr-collected"
            )
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, experiment="exp2-collection")

    assert [t.id for t in result] == ["tr-collected"]


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_experiment_scope_excludes_soft_deleted_collection_link():
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            session.add(
                ExperimentModel(id="exp2-collection", name="Collection", org_id="org1")
            )
            session.add(_trial("tr-collected", task_id="task1", experiment_id="exp1"))
            await session.flush()
            await _link_collection(
                session,
                experiment_id="exp2-collection",
                trial_id="tr-collected",
                deleted_at=datetime.now(timezone.utc),
            )
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, experiment="exp2-collection")

    assert result == []


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_experiment_scope_dedupes_trial_reachable_both_ways():
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            # Home experiment_id AND a redundant experiment_trials row both
            # point at exp1 -- the union must not double-return the trial.
            session.add(_trial("tr-both", task_id="task1", experiment_id="exp1"))
            await session.flush()
            await _link_collection(session, experiment_id="exp1", trial_id="tr-both")
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(session, experiment="exp1")

    assert [t.id for t in result] == ["tr-both"]


@requires_db
@pytest.mark.asyncio
async def test_resolve_cohort_experiment_scope_collection_links_still_honor_guarantees():
    """Probe exclusion, id ordering, and filter-before-limit must hold for
    trials reached only through `experiment_trials`, not just home trials."""
    async with _fresh_db() as maker:
        async with maker() as session:
            await _seed_org_experiment_task(session)
            session.add(
                ExperimentModel(id="exp2-collection", name="Collection", org_id="org1")
            )
            # "aaa" sorts first but isn't fetchable -- a SQL LIMIT applied
            # before the Python filter would return zero rows here.
            session.add(
                _trial(
                    "aaa-unfetchable",
                    task_id="task1",
                    experiment_id="exp1",
                    has_trajectory=False,
                )
            )
            session.add(
                _trial(
                    "bbb-probe", task_id="task1", experiment_id="exp1", is_probe=True
                )
            )
            session.add(_trial("ccc-real", task_id="task1", experiment_id="exp1"))
            await session.flush()
            for tid in ("aaa-unfetchable", "bbb-probe", "ccc-real"):
                await _link_collection(
                    session, experiment_id="exp2-collection", trial_id=tid
                )
            await session.commit()

        async with maker() as session:
            result = await resolve_cohort(
                session, experiment="exp2-collection", limit=5
            )

    assert [t.id for t in result] == ["ccc-real"]


def _ctx() -> TaskContext:
    return TaskContext(
        task_name="my-task",
        instruction=None,
        final_reward=1.0,
        model_used="claude-opus-4-8",
        verifier_output=None,
    )


# Named distinctly from the DB-backed `_trial` helper above (line 97) --
# same name would shadow it at module scope and break those tests.
def _summary_trial(trial_id="tr_a"):
    return SimpleNamespace(id=trial_id, reward=1.0)


def _traj() -> dict:
    return {
        "steps": [
            {
                "step_id": 1,
                "timestamp": "2026-04-30T12:00:00Z",
                "source": "agent",
                "model_name": "claude-opus-4-8",
                "message": "hi",
                "reasoning_content": None,
                "tool_calls": None,
                "observation": None,
                "metrics": None,
            }
        ]
    }


def _payload() -> str:
    return json.dumps(
        {
            "summary": "Fixed it.",
            "highlights": [{"step_id": 1, "title": "Repro", "why": "First."}],
            "components": [
                {"step_ids": [1], "trajectory_component": "debugging", "summary": "d"}
            ],
        }
    )


def _install_summary_client(monkeypatch, client):
    async def create(*args, **kwargs):
        return client

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.create_llm_client", create
    )


@pytest.mark.asyncio
async def test_summarize_trial_returns_full_record(monkeypatch):
    from oddish.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient
    from api.services.summary_dump import summarize_trial

    _install_summary_client(monkeypatch, FakeAnalyzerLLMClient(chunks=[_payload()]))
    record = await summarize_trial(
        _summary_trial(), _traj(), _ctx(), model="claude-opus-4-8", persist=False
    )

    assert record["trial_id"] == "tr_a"
    assert record["task_name"] == "my-task"
    assert record["final_reward"] == 1.0
    assert record["model"] == "claude-opus-4-8"
    assert record["schema_version"] == "5"
    assert record["status"] == "success"
    assert record["error"] is None
    assert isinstance(record["duration_s"], float)
    assert "my-task" in record["prompt"]
    assert record["raw"] == _payload()
    assert record["summary"]["summary"] == "Fixed it."


@pytest.mark.asyncio
async def test_summarize_trial_resolves_prompt_with_full_trial_scope(monkeypatch):
    from unittest.mock import AsyncMock

    from oddish.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient
    from api.services.summary_dump import summarize_trial

    load_prompt = AsyncMock(return_value=("TEMPLATE", 1, "prompt-test-id"))
    monkeypatch.setattr(
        "api.services.summarize_trajectory._load_summary_prompt", load_prompt
    )
    _install_summary_client(monkeypatch, FakeAnalyzerLLMClient(chunks=[_payload()]))
    trial = SimpleNamespace(
        id="trial_1",
        reward=1.0,
        org_id="org_1",
        billed_user_id="user_1",
        experiment_id="experiment_1",
        task_id="task_1",
    )

    await summarize_trial(
        trial, _traj(), _ctx(), model="claude-opus-4-8", persist=False
    )

    load_prompt.assert_awaited_once()
    assert load_prompt.await_args.kwargs == {
        "org_id": "org_1",
        "user_id": "user_1",
        "experiment_id": "experiment_1",
        "task_id": "task_1",
        "trial_id": "trial_1",
    }


@pytest.mark.asyncio
async def test_summarize_trial_records_raw_on_parse_failure(monkeypatch):
    """A revised taxonomy that fails to parse is exactly when raw matters most,
    and that is the path where block.run() raises."""
    from oddish.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient
    from api.services.summary_dump import summarize_trial

    _install_summary_client(
        monkeypatch, FakeAnalyzerLLMClient(chunks=["not json at all"])
    )
    record = await summarize_trial(
        _summary_trial(), _traj(), _ctx(), model="claude-opus-4-8", persist=False
    )

    assert record["status"] == "failed"
    assert record["error"] is not None
    assert record["raw"] == "not json at all"
    assert record["summary"] is None


@pytest.mark.asyncio
async def test_summarize_trial_does_not_persist_by_default(monkeypatch):
    from unittest.mock import AsyncMock

    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock
    from oddish.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient
    from api.services.summary_dump import summarize_trial

    s3 = AsyncMock()
    db = AsyncMock()
    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", s3)
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", db)
    _install_summary_client(monkeypatch, FakeAnalyzerLLMClient(chunks=[_payload()]))

    await summarize_trial(
        _summary_trial(), _traj(), _ctx(), model="claude-opus-4-8", persist=False
    )

    s3.assert_not_awaited()
    db.assert_not_awaited()


@pytest.mark.asyncio
async def test_summarize_trial_persists_when_persist_is_true(monkeypatch):
    """Counterpart to the persist=False test: an implementation that always
    stubbed the savers would satisfy that one alone."""
    from unittest.mock import AsyncMock

    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock
    from oddish.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient
    from api.services.summary_dump import summarize_trial

    s3 = AsyncMock()
    db = AsyncMock()
    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", s3)
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", db)
    _install_summary_client(monkeypatch, FakeAnalyzerLLMClient(chunks=[_payload()]))

    record = await summarize_trial(
        _summary_trial(), _traj(), _ctx(), model="claude-opus-4-8", persist=True
    )

    assert record["status"] == "success"
    s3.assert_awaited_once()
    db.assert_awaited_once()


@pytest.mark.asyncio
async def test_summarize_trial_propagates_cancellation(monkeypatch):
    import asyncio

    from oddish.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient
    from api.services.summary_dump import summarize_trial

    _install_summary_client(
        monkeypatch, FakeAnalyzerLLMClient(exc=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        await summarize_trial(
            _summary_trial(), _traj(), _ctx(), model="claude-opus-4-8", persist=False
        )


@pytest.mark.asyncio
async def test_summarize_trial_records_failure_raised_before_the_block_exists():
    """A trajectory that isn't a dict is rejected by TrajectoryInput inside
    build_summary_block -- before any block exists to carry status/error.
    That must still be a record, not an exception."""
    from api.services.summary_dump import summarize_trial

    record = await summarize_trial(
        _summary_trial(),
        ["not", "a", "dict"],
        _ctx(),
        model="claude-opus-4-8",
        persist=False,
    )

    assert record["trial_id"] == "tr_a"
    assert record["task_name"] == "my-task"
    assert record["status"] == "failed"
    assert record["error"] is not None
    assert record["prompt"] is None
    assert record["raw"] == ""
    assert record["summary"] is None
    assert record["duration_s"] is None


def _surrogate_payload() -> str:
    """Valid JSON whose text carries a lone surrogate: parses fine, but
    _persist's utf-8 encode raises -- from run()'s finally, after SUCCESS."""
    return _payload().replace("Fixed it.", "Fixed it." + "\ud800")


@pytest.mark.asyncio
async def test_summarize_trial_never_reports_success_without_a_summary(monkeypatch):
    from oddish.blocks.analyzer.analyzer_llm_client import FakeAnalyzerLLMClient
    from api.services.summary_dump import summarize_trial

    _install_summary_client(
        monkeypatch, FakeAnalyzerLLMClient(chunks=[_surrogate_payload()])
    )
    record = await summarize_trial(
        _summary_trial(), _traj(), _ctx(), model="claude-opus-4-8", persist=False
    )

    assert record["summary"] is None
    assert record["status"] == "failed"
    assert record["error"] is not None
    assert "UnicodeEncodeError" in record["error"]


class _RaisingAcloseClient:
    """A client whose block work succeeds but whose owned-client teardown
    fails -- distinct from a block-level failure."""

    def __init__(self, **kwargs):
        pass

    async def stream(self, prompt, *, system_prompt: str | None = None):
        yield _payload()

    async def aclose(self):
        raise RuntimeError("teardown blew up")


@pytest.mark.asyncio
async def test_summarize_trial_keeps_success_when_only_teardown_fails(monkeypatch):
    """A good summary must not be relabeled failed just because aclose()
    raised after the block already succeeded."""
    import oddish.blocks.analyzer.analyzer_llm_client as llm_mod
    from api.services.summary_dump import summarize_trial

    monkeypatch.setattr(llm_mod, "ApiAnalyzerLLMClient", _RaisingAcloseClient)

    # No `client=` kwarg -- summarize_trial must construct (and own, and
    # therefore aclose()) the client itself for this path to exercise.
    record = await summarize_trial(
        _summary_trial(),
        _traj(),
        _ctx(),
        model="claude-opus-4-8",
        persist=False,
    )

    assert record["status"] == "success"
    assert record["summary"] is not None
    assert record["summary"]["summary"] == "Fixed it."
    assert record["error"] is not None
    assert "teardown blew up" in record["error"]


# ---------------------------------------------------------------------------
# run_cohort -- no DB, no S3: resolve_cohort / trajectory / context are stubbed.
# ---------------------------------------------------------------------------


class _FakePromptClient:
    """Stands in for ApiAnalyzerLLMClient. The prompt carries the task name, so
    a per-trial verdict can be keyed off it without ordering assumptions."""

    def __init__(self, **kwargs):
        pass

    async def stream(self, prompt, *, system_prompt: str | None = None):
        yield "not json at all" if "task-bad" in prompt else _payload()

    async def aclose(self):
        return None


def _cohort_trial(trial_id, task_name):
    return SimpleNamespace(
        id=trial_id, reward=1.0, task=SimpleNamespace(name=task_name)
    )


def _stub_cohort(monkeypatch, cohort, *, prep_raises_for=()):
    import oddish.blocks.analyzer.analyzer_llm_client as llm_mod
    import api.services.summarize_trajectory as st
    import api.services.summary_dump as sd
    import oddish.core.trial_io as trial_io

    async def _resolve(_session, **_kw):
        return cohort

    async def _read(trial):
        if trial.id in prep_raises_for:
            raise RuntimeError(f"s3 blew up for {trial.id}")
        return _traj()

    async def _ctx_for(trial):
        return TaskContext(
            task_name=trial.task.name,
            instruction=None,
            final_reward=1.0,
            model_used="claude-opus-4-8",
            verifier_output=None,
        )

    monkeypatch.setattr(sd, "resolve_cohort", _resolve)
    monkeypatch.setattr(trial_io, "read_trial_trajectory", _read)
    monkeypatch.setattr(st, "build_task_context", _ctx_for)
    monkeypatch.setattr(llm_mod, "ApiAnalyzerLLMClient", _FakePromptClient)


@pytest.mark.asyncio
async def test_run_cohort_one_failing_trial_does_not_abort_the_cohort(monkeypatch):
    from api.services.summary_dump import run_cohort

    cohort = [
        _cohort_trial("tr-1", "task-good"),
        _cohort_trial("tr-2", "task-bad"),
        _cohort_trial("tr-3", "task-good"),
    ]
    _stub_cohort(monkeypatch, cohort)

    records = await run_cohort(None, experiment="exp1", model="claude-opus-4-8")

    assert [r["trial_id"] for r in records] == ["tr-1", "tr-2", "tr-3"]
    assert [r["status"] for r in records] == ["success", "failed", "success"]
    assert records[1]["raw"] == "not json at all"


@pytest.mark.asyncio
async def test_run_cohort_survives_summarize_trial_raising(monkeypatch):
    """Defense in depth for the un-guarded gather: even if summarize_trial
    regressed to raising, the other trials' records must survive."""
    import api.services.summary_dump as sd
    from api.services.summary_dump import run_cohort

    cohort = [_cohort_trial(f"tr-{i}", "task-good") for i in (1, 2, 3)]
    _stub_cohort(monkeypatch, cohort)

    async def _boom(trial, *_a, **_kw):
        if trial.id == "tr-2":
            raise RuntimeError("summarize_trial regressed")
        return {"trial_id": trial.id, "status": "success"}

    monkeypatch.setattr(sd, "summarize_trial", _boom)

    records = await run_cohort(None, experiment="exp1", model="claude-opus-4-8")

    assert [r["trial_id"] for r in records] == ["tr-1", "tr-2", "tr-3"]
    assert records[1]["status"] == "failed"
    assert "summarize_trial regressed" in records[1]["error"]


@pytest.mark.asyncio
async def test_run_cohort_prep_failure_yields_a_record_and_continues(monkeypatch):
    from api.services.summary_dump import run_cohort

    cohort = [
        _cohort_trial("tr-1", "task-good"),
        _cohort_trial("tr-2", "task-good"),
        _cohort_trial("tr-3", "task-good"),
    ]
    _stub_cohort(monkeypatch, cohort, prep_raises_for={"tr-2"})

    records = await run_cohort(None, experiment="exp1", model="claude-opus-4-8")

    # In cohort order, with the prep failure holding its own slot.
    assert [r["trial_id"] for r in records] == ["tr-1", "tr-2", "tr-3"]
    assert [r["status"] for r in records] == ["success", "failed", "success"]
    assert "s3 blew up for tr-2" in records[1]["error"]
    assert records[1]["task_name"] == "task-good"
    assert records[1]["prompt"] is None
    assert records[1]["summary"] is None


@pytest.mark.asyncio
async def test_run_cohort_skips_trials_with_no_trajectory_without_recording(
    monkeypatch,
):
    import oddish.core.trial_io as trial_io
    from api.services.summary_dump import run_cohort

    cohort = [_cohort_trial("tr-1", "task-good"), _cohort_trial("tr-2", "task-good")]
    _stub_cohort(monkeypatch, cohort)

    async def _read(trial):
        return None if trial.id == "tr-2" else _traj()

    monkeypatch.setattr(trial_io, "read_trial_trajectory", _read)

    records = await run_cohort(None, experiment="exp1", model="claude-opus-4-8")

    assert [r["trial_id"] for r in records] == ["tr-1"]


@pytest.mark.asyncio
async def test_run_cohort_reports_unknown_explicit_trial_id_as_skipped(monkeypatch):
    from api.services.summary_dump import run_cohort

    # resolve_cohort is stubbed to always return this cohort, standing in for
    # it having already dropped "tr-missing" -- run_cohort must diff against
    # what was requested to notice.
    cohort = [_cohort_trial("tr-1", "task-good"), _cohort_trial("tr-2", "task-good")]
    _stub_cohort(monkeypatch, cohort)

    result = await run_cohort(
        None,
        trials=["tr-1", "tr-missing", "tr-2"],
        model="claude-opus-4-8",
    )

    assert [r["trial_id"] for r in result] == ["tr-1", "tr-2"]
    assert result.skipped == [{"trial_id": "tr-missing", "reason": "no such trial"}]


@pytest.mark.asyncio
async def test_run_cohort_reports_missing_trajectory_as_skipped(monkeypatch):
    import oddish.core.trial_io as trial_io
    from api.services.summary_dump import run_cohort

    cohort = [_cohort_trial("tr-1", "task-good"), _cohort_trial("tr-2", "task-good")]
    _stub_cohort(monkeypatch, cohort)

    async def _read(trial):
        return None if trial.id == "tr-2" else _traj()

    monkeypatch.setattr(trial_io, "read_trial_trajectory", _read)

    result = await run_cohort(None, experiment="exp1", model="claude-opus-4-8")

    assert result.skipped == [{"trial_id": "tr-2", "reason": "no fetchable trajectory"}]


@pytest.mark.asyncio
async def test_run_cohort_skips_do_not_abort_and_other_records_still_return(
    monkeypatch,
):
    """Both an unknown id and a missing trajectory in one run: neither aborts
    the cohort, and the surviving trials still get records."""
    import oddish.core.trial_io as trial_io
    from api.services.summary_dump import run_cohort

    cohort = [
        _cohort_trial("tr-1", "task-good"),
        _cohort_trial("tr-2", "task-good"),
        _cohort_trial("tr-3", "task-good"),
    ]
    _stub_cohort(monkeypatch, cohort)

    async def _read(trial):
        return None if trial.id == "tr-2" else _traj()

    monkeypatch.setattr(trial_io, "read_trial_trajectory", _read)

    result = await run_cohort(
        None,
        trials=["tr-1", "tr-2", "tr-3", "tr-missing"],
        model="claude-opus-4-8",
    )

    assert [r["trial_id"] for r in result] == ["tr-1", "tr-3"]
    assert len(result.skipped) == 2
    assert {"trial_id": "tr-missing", "reason": "no such trial"} in result.skipped
    assert {"trial_id": "tr-2", "reason": "no fetchable trajectory"} in result.skipped


# ---------------------------------------------------------------------------
# Output-token cap (shared by prod + harness)
# ---------------------------------------------------------------------------


class _CapturingClient:
    """Stands in for ApiAnalyzerLLMClient to record its construction kwargs."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs

    async def stream(self, prompt, *, system_prompt=None):
        yield _payload()

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_prod_and_harness_use_the_same_output_cap(monkeypatch):
    """A dump is only trustworthy if it sends what prod sends. The 2048 cap
    truncated long trajectories mid-JSON, so both sides must move together."""
    import oddish.blocks.analyzer.analyzer_llm_client as llm_mod
    from api.services.summarize_trajectory import (
        SUMMARY_MAX_TOKENS,
        TaskContext,
        generate,
    )
    from api.services.summary_dump import summarize_trial

    from unittest.mock import AsyncMock

    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock

    monkeypatch.setattr(llm_mod, "ApiAnalyzerLLMClient", _CapturingClient)
    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", AsyncMock())
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", AsyncMock())

    ctx = TaskContext(
        task_name="t",
        instruction=None,
        final_reward=None,
        model_used=None,
        verifier_output=None,
    )

    await generate(
        _traj(),
        ctx,
        analyzer_id="tr_x",
        prompt_template="TEMPLATE",
        prompt_version=1,
        prompt_id="prompt-test-id",
    )
    prod_kwargs = _CapturingClient.last_kwargs

    await summarize_trial(
        _summary_trial(),
        _traj(),
        ctx,
        model="claude-sonnet-5",
        persist=False,
    )
    harness_kwargs = _CapturingClient.last_kwargs

    assert prod_kwargs["max_tokens"] == SUMMARY_MAX_TOKENS
    assert harness_kwargs["max_tokens"] == SUMMARY_MAX_TOKENS
    assert SUMMARY_MAX_TOKENS > 2048
