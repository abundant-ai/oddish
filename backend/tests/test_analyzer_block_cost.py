"""Cost accounting for AnalyzerBlock: every block's LLM spend lands in
``analysis_costs``, labelled with the block's own kind."""

from types import SimpleNamespace

import pytest

from api.services.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from api.services.blocks.analyzer.analyzer_llm_client import (
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


def _session(monkeypatch, added: list, trial_row=TRIAL_ROW):
    class _Result:
        def first(self):
            return trial_row

    class _FakeSession:
        def add(self, obj):
            added.append(obj)

        async def execute(self, *a, **k):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_block.get_session",
        lambda: _FakeSession(),
    )


def _make_block(**over):
    kw = dict(
        analyzer_type=AnalyzerType.TRAJECTORY_SUMMARY,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(input={"x": 1}),
        prompt="summarize",
        analyzer_id="trial-1",
    )
    kw.update(over)
    return AnalyzerBlock(**kw)


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
async def test_non_trial_analyzer_id_leaves_attribution_null(monkeypatch):
    """Cohort blocks carry a report id, not a trial id -- the lookup misses and
    must not invent a trial_id."""
    added: list = []
    _session(monkeypatch, added, trial_row=None)
    b = _make_block(
        analyzer_type=AnalyzerType.TRAJECTORY_FAILURE_ANALYSIS,
        analyzer_id="report-9",
    )
    b.usage = USAGE
    await b.record_cost()
    row = added[0]
    assert row.job_kind == "trajectory_failure_analysis"
    assert row.trial_id is None
    assert row.org_id is None
    assert row.cost_usd == 0.42


@pytest.mark.asyncio
async def test_record_cost_never_raises(monkeypatch, caplog):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_block.get_session", _boom
    )
    b = _make_block()
    b.usage = USAGE
    await b.record_cost()  # must NOT raise
    assert "record_cost" in caplog.text


@pytest.mark.asyncio
async def test_run_captures_usage_from_client_and_persists(monkeypatch):
    added: list = []
    _session(monkeypatch, added)
    monkeypatch.setattr(
        "api.services.blocks.analyzer.analyzer_block.get_storage_client",
        lambda: SimpleNamespace(
            upload_bytes=lambda *a, **k: _noop()
        ),
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
        "api.services.blocks.analyzer.analyzer_block.get_storage_client",
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
