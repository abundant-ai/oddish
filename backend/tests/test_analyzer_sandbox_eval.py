"""sandbox_eval_rows buckets DB-only, then runs one sandbox per cohort."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from oddish.evals.analyzer.schemas import AnalyzerEvalConfig, CapabilityProposal, Finding
from oddish.evals.analyzer.taxonomy import Capability, Category, Taxonomy
from oddish.evals.primitives import SubAnalysis

pytestmark = pytest.mark.asyncio


def _sa(trial_id, classification) -> SubAnalysis:
    return SubAnalysis(
        trial_id=trial_id, trajectory_link=f"/tasks/t1/probe/{trial_id}",
        classification=classification, subtype="1a", evidence="e",
        root_cause="rc", recommendation="r",
    )


def _hosts(*trial_ids) -> dict[str, dict]:
    return {
        t: {
            "trajectory_link": f"/tasks/t1/probe/{t}", "model": "m",
            "classification": "BAD_FAILURE", "subtype": "1a",
            "task_id": "task-1", "task_path": "tasks/t1",
        }
        for t in trial_ids
    }


def _finding(trial_id, bucket) -> Finding:
    return Finding(
        trial_id=trial_id, bucket=bucket, subcategory="1a", evidence_quote="q",
        step_ids=[], root_cause="rc", headroom_signal="h",
        trajectory_link=f"/tasks/t1/probe/{trial_id}",
    )


_TAX = Taxonomy(
    categories=(Category("verification", "Verification failures", "d", 0),),
    capabilities=(Capability("agent-early-stop", "Agent Early Stop",
                             "Stops early.", "apex-swe.",
                             primary_category="verification"),),
)


@pytest.fixture
def patched(monkeypatch):
    """Stub everything outside the unit: bucketing inputs, creds, CLI, sandboxes."""
    from worker import analyzer_sandbox as m

    calls = []
    revoked = []

    async def fake_run_cohort(client, runtime, *, bucket, cohort, **kw):
        calls.append({"bucket": bucket, "cohort": [sa.trial_id for sa in cohort]})
        if bucket == "bad":
            return [_finding("bad-1", "bad")], {"bad_failure_content": "# Bad"}, []
        return [_finding("good-1", "good")], {
            "good_failure_content": "# Good",
            "universal_capabilities_content": "# Caps",
            "headroom_analysis": "# Head",
        }, []

    monkeypatch.setattr(m, "run_cohort", fake_run_cohort)
    monkeypatch.setattr(m, "_read_cli_source", lambda: b"cli")
    monkeypatch.setattr(m, "_daytona_client", lambda: object())
    monkeypatch.setattr(m, "_runtime", lambda: object())

    async def fake_creds(rows, analyzer_id):
        return "key-id-1", "https://api.example", "key"

    async def fake_revoke(key_id, analyzer_id):
        revoked.append(key_id)

    monkeypatch.setattr(m, "_resolve_api_creds", fake_creds)
    monkeypatch.setattr(m, "revoke_probe_creds", fake_revoke)

    async def fake_load_taxonomy(session):
        return _TAX

    monkeypatch.setattr(m, "load_taxonomy", fake_load_taxonomy)

    @asynccontextmanager
    async def fake_get_session():
        yield None

    monkeypatch.setattr(m, "get_session", fake_get_session)
    return m, calls, revoked


async def test_runs_one_sandbox_per_cohort_and_merges_sections(patched, monkeypatch):
    m, calls, _ = patched
    monkeypatch.setattr(
        m, "_gather", lambda rows: (
            [_sa("bad-1", "BAD_FAILURE"), _sa("good-1", "GOOD_FAILURE")],
            {"bad-1": "oracle"}, _hosts("bad-1", "good-1"),
        ),
    )
    out = await m.sandbox_eval_rows([("r1", "t"), ("r2", "t")], AnalyzerEvalConfig(), "a1")

    assert sorted(c["bucket"] for c in calls) == ["bad", "good"]
    assert out.sections == {
        "bad": "# Bad", "good": "# Good",
        "capabilities": "# Caps", "headroom": "# Head",
    }
    assert out.counts == {"trials": 2, "bad": 1, "good": 1}
    assert len(out.findings) == 2


async def test_empty_cohort_provisions_no_sandbox(patched, monkeypatch):
    m, calls, _ = patched
    monkeypatch.setattr(
        m, "_gather",
        lambda rows: ([_sa("bad-1", "BAD_FAILURE")], {"bad-1": "o"}, _hosts("bad-1")),
    )
    out = await m.sandbox_eval_rows([("r1", "t")], AnalyzerEvalConfig(), "a1")
    assert [c["bucket"] for c in calls] == ["bad"]
    assert out.sections["good"] == ""
    assert out.sections["headroom"] == ""


async def test_both_cohorts_empty_provisions_nothing(patched, monkeypatch):
    m, calls, _ = patched
    monkeypatch.setattr(m, "_gather", lambda rows: ([], {}, {}))
    out = await m.sandbox_eval_rows([("r1", "t")], AnalyzerEvalConfig(), "a1")
    assert calls == []
    assert out.sections == {"bad": "", "good": "", "capabilities": "", "headroom": ""}
    assert out.findings == []


async def test_cohort_failure_fails_the_job(patched, monkeypatch):
    """No fallback to the API path: it would silently re-introduce the S3 load
    and the context overflow this exists to avoid."""
    m, _, _ = patched
    monkeypatch.setattr(
        m, "_gather",
        lambda rows: ([_sa("bad-1", "BAD_FAILURE")], {"bad-1": "o"}, _hosts("bad-1")),
    )

    async def boom(client, runtime, *, bucket, cohort, **kw):
        raise RuntimeError("sandbox exploded")

    monkeypatch.setattr(m, "run_cohort", boom)
    with pytest.raises(RuntimeError, match="sandbox exploded"):
        await m.sandbox_eval_rows([("r1", "t")], AnalyzerEvalConfig(), "a1")


async def test_failure_waits_for_siblings_to_tear_down(patched, monkeypatch):
    """A bad cohort's fast raise must not cut the good cohort's run short: the
    call must not return until every cohort has settled, so the survivor's own
    `finally` teardown always runs on a live loop. No `asyncio.sleep` grace
    period here after the call returns -- production gets none either, so if
    this test needs one to pass, the fix doesn't actually work."""
    m, _, _ = patched
    monkeypatch.setattr(
        m, "_gather", lambda rows: (
            [_sa("bad-1", "BAD_FAILURE"), _sa("good-1", "GOOD_FAILURE")],
            {"bad-1": "o"}, _hosts("bad-1", "good-1"),
        ),
    )
    good_finished = False

    async def flaky(client, runtime, *, bucket, cohort, **kw):
        nonlocal good_finished
        if bucket == "bad":
            raise RuntimeError("sandbox exploded")
        await asyncio.sleep(0.05)
        good_finished = True
        return [_finding("good-1", "good")], {"good_failure_content": "# Good"}, []

    monkeypatch.setattr(m, "run_cohort", flaky)
    with pytest.raises(RuntimeError, match="sandbox exploded"):
        await m.sandbox_eval_rows([("r1", "t"), ("r2", "t")], AnalyzerEvalConfig(), "a1")
    assert good_finished


async def test_first_cohort_exception_propagates_not_a_later_one(patched, monkeypatch):
    """When both cohorts fail, the one that raises first is what the caller
    sees -- not whichever happens to settle last in the gathered list."""
    m, _, _ = patched
    monkeypatch.setattr(
        m, "_gather", lambda rows: (
            [_sa("bad-1", "BAD_FAILURE"), _sa("good-1", "GOOD_FAILURE")],
            {"bad-1": "o"}, _hosts("bad-1", "good-1"),
        ),
    )

    async def both_fail(client, runtime, *, bucket, cohort, **kw):
        if bucket == "bad":
            raise RuntimeError("bad cohort exploded")
        await asyncio.sleep(0.02)
        raise RuntimeError("good cohort exploded")

    monkeypatch.setattr(m, "run_cohort", both_fail)
    with pytest.raises(RuntimeError, match="bad cohort exploded"):
        await m.sandbox_eval_rows([("r1", "t"), ("r2", "t")], AnalyzerEvalConfig(), "a1")


async def test_non_empty_cohort_with_zero_findings_is_fatal(patched, monkeypatch):
    """Blank sections that look like a completed analysis are worse than a failure."""
    m, _, _ = patched
    monkeypatch.setattr(
        m, "_gather",
        lambda rows: ([_sa("bad-1", "BAD_FAILURE")], {"bad-1": "o"}, _hosts("bad-1")),
    )

    async def empty(client, runtime, *, bucket, cohort, **kw):
        return [], {"bad_failure_content": ""}, []

    monkeypatch.setattr(m, "run_cohort", empty)
    with pytest.raises(RuntimeError, match="no findings"):
        await m.sandbox_eval_rows([("r1", "t")], AnalyzerEvalConfig(), "a1")


async def test_api_key_revoked_on_success(patched, monkeypatch):
    m, _, revoked = patched
    monkeypatch.setattr(
        m, "_gather",
        lambda rows: ([_sa("bad-1", "BAD_FAILURE")], {"bad-1": "o"}, _hosts("bad-1")),
    )
    await m.sandbox_eval_rows([("r1", "t")], AnalyzerEvalConfig(), "a1")
    assert revoked == ["key-id-1"]


async def test_api_key_revoked_on_cohort_failure(patched, monkeypatch):
    m, _, revoked = patched
    monkeypatch.setattr(
        m, "_gather",
        lambda rows: ([_sa("bad-1", "BAD_FAILURE")], {"bad-1": "o"}, _hosts("bad-1")),
    )

    async def boom(client, runtime, *, bucket, cohort, **kw):
        raise RuntimeError("sandbox exploded")

    monkeypatch.setattr(m, "run_cohort", boom)
    with pytest.raises(RuntimeError, match="sandbox exploded"):
        await m.sandbox_eval_rows([("r1", "t")], AnalyzerEvalConfig(), "a1")
    assert revoked == ["key-id-1"]


async def test_missing_anthropic_key_fails_before_provisioning_anything(
    patched, monkeypatch
):
    """A missing key must not cost two sandboxes and a minted probe key first --
    the agent would only fail once inside, opaquely, at inference time."""
    m, calls, revoked = patched
    monkeypatch.setattr(
        m, "_gather",
        lambda rows: ([_sa("bad-1", "BAD_FAILURE")], {"bad-1": "o"}, _hosts("bad-1")),
    )
    minted = []

    async def track_creds(rows, analyzer_id):
        minted.append(analyzer_id)
        return "key-id-1", "https://api.example", "key"

    monkeypatch.setattr(m, "_resolve_api_creds", track_creds)
    monkeypatch.setattr(m.settings, "anthropic_api_key", "")

    with pytest.raises(RuntimeError, match="anthropic_api_key is unset"):
        await m.sandbox_eval_rows([("r1", "t")], AnalyzerEvalConfig(), "a1")
    assert calls == [], "no cohort sandbox should have been provisioned"
    assert minted == [], "no probe key should have been minted"


async def test_sandbox_eval_carries_proposals_out_on_the_output(patched, monkeypatch):
    """Proposals must ride out on the output: sandbox_eval_rows holds no
    session, and _store() is the only thing that writes."""
    m, _, _ = patched
    monkeypatch.setattr(
        m, "_gather",
        lambda rows: (
            [_sa("bad-1", "BAD_FAILURE"), _sa("good-1", "GOOD_FAILURE")],
            {"bad-1": "o"}, _hosts("bad-1", "good-1"),
        ),
    )
    prop = CapabilityProposal(
        slug="hypothesis-fixation", name="Hypothesis Fixation", description="d",
        categories=["verification"], trial_ids=["good-1"],
    )

    async def fake_run_cohort(client, runtime, *, bucket, cohort, **kw):
        if bucket == "bad":
            return [_finding("bad-1", "bad")], {"bad_failure_content": "# Bad"}, []
        return (
            [_finding("good-1", "good")],
            {"good_failure_content": "# Good",
             "universal_capabilities_content": "# Caps",
             "headroom_analysis": "# Head"},
            [prop],
        )

    monkeypatch.setattr(m, "run_cohort", fake_run_cohort)
    out = await m.sandbox_eval_rows(
        [("r1", "t"), ("r2", "t")], AnalyzerEvalConfig(), "an-1"
    )

    assert out.proposals == [prop]


async def test_sandbox_eval_snapshots_the_taxonomy_it_ran_against(patched, monkeypatch):
    """Without the snapshot, editing a capability silently rewrites the meaning
    of this run's breakdown after the fact."""
    m, _, _ = patched
    monkeypatch.setattr(
        m, "_gather",
        lambda rows: ([_sa("bad-1", "BAD_FAILURE")], {"bad-1": "o"}, _hosts("bad-1")),
    )
    out = await m.sandbox_eval_rows([("r1", "t")], AnalyzerEvalConfig(), "an-1")

    assert len(out.taxonomy_version) == 12
    assert [c["slug"] for c in out.taxonomy_snapshot["capabilities"]] == [
        "agent-early-stop"
    ]


async def test_zero_work_still_snapshots_the_taxonomy(patched, monkeypatch):
    """A run that found no failures still ran against a taxonomy."""
    m, _, _ = patched
    monkeypatch.setattr(m, "_gather", lambda rows: ([], {}, {}))
    out = await m.sandbox_eval_rows([("r1", "t")], AnalyzerEvalConfig(), "an-1")

    assert len(out.taxonomy_version) == 12
    assert out.proposals == []


async def test_handler_subclass_uses_the_sandbox_strategy():
    from worker.analyzer_sandbox import SandboxAnalyzerJobHandler
    from worker import analyzer_sandbox as m
    from oddish.db import WorkerJobKind

    assert SandboxAnalyzerJobHandler.kind == WorkerJobKind.ANALYZER
    assert SandboxAnalyzerJobHandler.eval_rows_fn is m.sandbox_eval_rows
