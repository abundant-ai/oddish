"""Cost accounting for AnalyzerBlock: every block's LLM spend lands in
``analysis_costs``, labelled with the block's own kind."""

from types import SimpleNamespace

import pytest

from oddish.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from oddish.blocks.analyzer.analyzer_llm_client import (
    FakeAnalyzerLLMClient,
    LLMClientType,
)
from oddish.analyze.analysis_cost import AnalysisUsage

USAGE = AnalysisUsage(
    cost_usd=0.42,
    input_tokens=1600,
    output_tokens=200,
    cache_read_tokens=500,
    cache_write_tokens=100,
    model="claude-opus-4-8",
    source="estimated",
)

TRIAL_ROW = SimpleNamespace(
    id="trial-1", org_id="org-1", experiment_id="exp-1", billed_user_id="user-1"
)


ANALYZER_ROW = SimpleNamespace(org_id="org-9", owner_user_id="user-9")
TASK_ROW = SimpleNamespace(org_id="org-task", created_by_user_id="user-task")


class _StopBeforeRun(Exception):
    """Aborts generate() at the construction site, before any real IO."""


def _session(
    monkeypatch, added: list, trial_row=TRIAL_ROW, analyzer_row=None, task_row=None
):
    """Fake session whose SELECTs answer by target table, so the trial lookup
    and the analyzers fallback can be distinguished."""

    class _Result:
        def __init__(self, row):
            self._row = row

        def first(self):
            return self._row

    class _FakeSession:
        def add(self, obj):
            added.append(obj)

        async def execute(self, stmt, *a, **k):
            tables = {t.name for t in stmt.get_final_froms()}
            if "trials" in tables:
                return _Result(trial_row)
            if "tasks" in tables:
                return _Result(task_row)
            if "analyzers" in tables:
                return _Result(analyzer_row)
            raise AssertionError(f"unexpected select against {tables}")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.get_session",
        lambda: _FakeSession(),
    )


def _make_block(**over):
    client = over.pop("client", None)
    kw = dict(
        analyzer_type=AnalyzerType.TRAJECTORY_SUMMARY,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(input={"x": 1}),
        prompt="summarize",
        analyzer_id="trial-1",
    )
    kw.update(over)
    block = AnalyzerBlock(**kw)
    if client is not None:

        async def _create_client():
            return client

        block._create_client = _create_client
    return block


@pytest.mark.asyncio
async def test_job_kind_is_the_block_kind(monkeypatch):
    added: list = []
    _session(monkeypatch, added)
    b = _make_block()
    b.usage = USAGE
    await b.record_cost()
    assert added[0].job_kind == "trajectory_summary"


@pytest.mark.asyncio
async def test_job_kind_follows_a_different_kind(monkeypatch):
    """The kind is read off the block, not hardcoded per call site."""
    added: list = []
    _session(monkeypatch, added)
    b = _make_block(analyzer_type=AnalyzerType.HEADROOM_ANALYSIS)
    b.usage = USAGE
    await b.record_cost()
    assert added[0].job_kind == "headroom_analysis"


@pytest.mark.asyncio
async def test_records_usage_and_attribution(monkeypatch):
    added: list = []
    _session(monkeypatch, added)
    b = _make_block()
    b.usage = USAGE
    await b.record_cost()
    row = added[0]
    assert (row.trial_id, row.org_id) == ("trial-1", "org-1")


@pytest.mark.asyncio
async def test_post_trial_cost_prefers_trial_attribution_over_task(monkeypatch):
    added: list = []
    _session(monkeypatch, added, task_row=TASK_ROW)
    block = _make_block(
        analyzer_type=AnalyzerType.POST_TRIAL,
        task_id="task-1",
    )
    block.usage = USAGE
    await block.record_cost()
    row = added[0]
    assert row.job_kind == "post_trial"
    assert row.trial_id == "trial-1"
    assert row.task_id == "task-1"
    assert row.org_id == "org-1"
    assert row.experiment_id == "exp-1"
    assert (row.experiment_id, row.billed_user_id) == ("exp-1", "user-1")
    assert row.cost_usd == 0.42
    assert row.input_tokens == 1600
    assert row.output_tokens == 200
    assert row.cache_read_tokens == 500
    assert row.cache_write_tokens == 100
    assert row.model == "claude-opus-4-8"
    assert row.cost_source == "estimated"


@pytest.mark.asyncio
async def test_no_usage_writes_nothing(monkeypatch):
    added: list = []
    _session(monkeypatch, added)
    b = _make_block()
    await b.record_cost()
    assert added == []


@pytest.mark.asyncio
async def test_cohort_block_is_attributed_via_the_analyzers_table(monkeypatch):
    """Cohort blocks carry an analyzers-row id, not a trial id. Spend must be
    charged to the org/user who triggered the report, not left unattributed --
    but no trial_id may be invented, since the block spans many trials."""
    added: list = []
    _session(monkeypatch, added, trial_row=None, analyzer_row=ANALYZER_ROW)
    b = _make_block(
        analyzer_type=AnalyzerType.TRAJECTORY_FAILURE_ANALYSIS,
        analyzer_id="report-9",
    )
    b.usage = USAGE
    await b.record_cost()
    row = added[0]
    assert row.job_kind == "trajectory_failure_analysis"
    assert row.org_id == "org-9"
    assert row.billed_user_id == "user-9"
    assert row.trial_id is None
    assert row.analyzer_id == "report-9"


@pytest.mark.asyncio
async def test_task_qa_block_is_attributed_via_the_tasks_table(monkeypatch):
    added: list = []
    _session(monkeypatch, added, trial_row=None, task_row=TASK_ROW)
    block = _make_block(
        analyzer_type=AnalyzerType.PRE_TRIAL,
        analyzer_id=None,
        task_id="task-9",
    )
    block.usage = USAGE
    await block.record_cost()
    row = added[0]
    assert row.job_kind == "pre_trial"
    assert row.org_id == "org-task"
    assert row.billed_user_id == "user-task"
    assert row.trial_id is None
    assert row.experiment_id is None
    assert row.cost_usd == 0.42
    assert row.task_id == "task-9"
    assert row.analyzer_id is None


@pytest.mark.asyncio
async def test_trial_lookup_wins_and_skips_the_analyzers_fallback(monkeypatch):
    added: list = []
    _session(monkeypatch, added, analyzer_row=ANALYZER_ROW)
    b = _make_block()
    b.usage = USAGE
    await b.record_cost()
    row = added[0]
    assert (row.org_id, row.billed_user_id) == ("org-1", "user-1")
    assert row.trial_id == "trial-1"


@pytest.mark.asyncio
async def test_trial_fallback_preserves_explicit_task_link(monkeypatch):
    """A stale task lookup must not erase the subject copied to the ledger."""
    added: list = []
    _session(monkeypatch, added, task_row=None)
    block = _make_block(task_id="task-missing")
    block.usage = USAGE

    await block.record_cost()

    assert added[0].trial_id == "trial-1"
    assert added[0].task_id == "task-missing"


@pytest.mark.asyncio
async def test_triggering_user_outranks_the_trial_owner(monkeypatch):
    """A summary generates lazily on view, so the viewer caused the spend --
    billing the trial's runner would charge the wrong person."""
    added: list = []
    _session(monkeypatch, added)
    b = _make_block(triggered_by_user_id="viewer-7")
    b.usage = USAGE
    await b.record_cost()
    row = added[0]
    assert row.billed_user_id == "viewer-7"
    # Everything else still comes off the trial.
    assert (row.trial_id, row.org_id, row.experiment_id) == (
        "trial-1",
        "org-1",
        "exp-1",
    )


@pytest.mark.asyncio
async def test_trial_owner_used_when_no_triggering_user(monkeypatch):
    """Internally-driven runs (workers, graph builder) have no request user."""
    added: list = []
    _session(monkeypatch, added)
    b = _make_block(triggered_by_user_id=None)
    b.usage = USAGE
    await b.record_cost()
    assert added[0].billed_user_id == "user-1"


@pytest.mark.asyncio
async def test_triggering_user_outranks_the_analyzer_owner(monkeypatch):
    added: list = []
    _session(monkeypatch, added, trial_row=None, analyzer_row=ANALYZER_ROW)
    b = _make_block(analyzer_id="report-9", triggered_by_user_id="viewer-7")
    b.usage = USAGE
    await b.record_cost()
    row = added[0]
    assert row.billed_user_id == "viewer-7"
    assert row.org_id == "org-9"


@pytest.mark.asyncio
async def test_triggering_user_recorded_even_when_id_matches_nothing(monkeypatch):
    added: list = []
    _session(monkeypatch, added, trial_row=None, analyzer_row=None)
    b = _make_block(analyzer_id="ghost-1", triggered_by_user_id="viewer-7")
    b.usage = USAGE
    await b.record_cost()
    assert added[0].billed_user_id == "viewer-7"


@pytest.mark.asyncio
async def test_unknown_id_leaves_attribution_null(monkeypatch):
    """An id in neither table records the spend rather than dropping it."""
    added: list = []
    _session(monkeypatch, added, trial_row=None, analyzer_row=None)
    b = _make_block(analyzer_id="ghost-1")
    b.usage = USAGE
    await b.record_cost()
    row = added[0]
    assert row.trial_id is None
    assert row.org_id is None
    assert row.billed_user_id is None
    assert row.cost_usd == 0.42


@pytest.mark.asyncio
async def test_record_cost_never_raises(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("oddish.blocks.analyzer.analyzer_block.get_session", _boom)
    b = _make_block()
    b.usage = USAGE
    await b.record_cost()  # must NOT raise
    assert "record_cost" in caplog.text


@pytest.mark.asyncio
async def test_run_captures_usage_from_client_and_persists(monkeypatch):
    added: list = []
    _session(monkeypatch, added)
    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.get_storage_client",
        lambda: SimpleNamespace(upload_bytes=lambda *a, **k: _noop()),
    )
    client = FakeAnalyzerLLMClient(chunks=["hello"], last_usage=USAGE)
    b = _make_block(client=client)
    await b.run()
    assert b.usage is USAGE
    # save_to_db row plus the cost row.
    kinds = [getattr(o, "job_kind", None) for o in added]
    assert "trajectory_summary" in kinds


@pytest.mark.asyncio
async def test_failed_block_still_records_spend(monkeypatch):
    """Tokens are spent even when the stream blows up mid-flight."""
    added: list = []
    _session(monkeypatch, added)
    monkeypatch.setattr(
        "oddish.blocks.analyzer.analyzer_block.get_storage_client",
        lambda: SimpleNamespace(upload_bytes=lambda *a, **k: _noop()),
    )
    client = FakeAnalyzerLLMClient(
        chunks=["partial"], exc=RuntimeError("boom"), last_usage=USAGE
    )
    b = _make_block(client=client)
    with pytest.raises(RuntimeError):
        await b.run()
    assert [o for o in added if getattr(o, "job_kind", None) == "trajectory_summary"]


async def _noop():
    return None


def test_summary_block_carries_the_triggering_user():
    """build_summary_block is the shared construction site; if it accepts the
    param but drops it on the floor, every summary silently reverts to billing
    the trial's runner. Assert it reaches the block, not just the signature."""
    from api.services.summarize_trajectory import TaskContext, build_summary_block

    block = build_summary_block(
        {"steps": []},
        TaskContext(
            task_name="my-task",
            instruction=None,
            final_reward=1.0,
            model_used="claude-opus-4-8",
            verifier_output=None,
        ),
        analyzer_id="trial-1",
        model="claude-opus-4-8",
        triggered_by_user_id="viewer-7",
        prompt_template="INSTRUCTIONS",
        prompt_version=1,
        prompt_id="prompt-test-id",
    )
    assert block.triggered_by_user_id == "viewer-7"


@pytest.mark.asyncio
async def test_generate_forwards_the_triggering_user_to_the_block():
    """generate() is the production entry point; it must not swallow the param
    between get_or_generate_summary and build_summary_block."""
    from api.services import summarize_trajectory

    seen = {}
    real = summarize_trajectory.build_summary_block

    def _spy(*a, **kw):
        seen.update(kw)
        # Stop here: running the block would reach real S3/DB.
        raise _StopBeforeRun

    summarize_trajectory.build_summary_block = _spy
    try:
        await summarize_trajectory.generate(
            {"steps": []},
            summarize_trajectory.TaskContext(
                task_name="my-task",
                instruction=None,
                final_reward=1.0,
                model_used="claude-opus-4-8",
                verifier_output=None,
            ),
            analyzer_id="trial-1",
            triggered_by_user_id="viewer-7",
            prompt_template="INSTRUCTIONS",
            prompt_version=1,
            prompt_id="prompt-test-id",
        )
    except _StopBeforeRun:
        pass
    finally:
        summarize_trajectory.build_summary_block = real
    assert seen.get("triggered_by_user_id") == "viewer-7"


def test_summary_routes_pass_the_requesting_user():
    """The user is only knowable at the route; the whole chain is pointless if
    the handlers don't hand it over."""
    from api.routers import trials

    src = open(trials.__file__).read()
    assert src.count("triggered_by_user_id=auth.user_id") == 2, (
        "both the summary and graph routes must attribute spend to the caller"
    )
    assert "get_or_generate_summary(session, attached_trial)" not in src, (
        "a bare call site is left, billing the trial owner instead of the viewer"
    )
