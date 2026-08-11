import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from api.services import cohort_comparison as cc
from api.services.cohort_comparison import MIN_COHORT, is_stale


def test_is_stale_when_cohort_hash_changed():
    block_meta = {"cohort_hash": "aaa", "schema_version": 1, "task_version_id": "v1"}
    assert (
        is_stale(block_meta, current_hash="bbb", schema_version=1, task_version_id="v1")
        is True
    )


def test_is_stale_when_schema_version_changed():
    # Keying freshness on schema_version alone is what left trajectory
    # summaries serving a retired vocabulary indefinitely; both must match.
    block_meta = {"cohort_hash": "aaa", "schema_version": 1, "task_version_id": "v1"}
    assert (
        is_stale(block_meta, current_hash="aaa", schema_version=2, task_version_id="v1")
        is True
    )


def test_is_stale_when_task_version_changed():
    # Block rows are now looked up by the stable task_id, not the version id,
    # so the version has to be checked explicitly rather than relying on the
    # cohort hash alone to catch a version switch.
    block_meta = {"cohort_hash": "aaa", "schema_version": 1, "task_version_id": "v1"}
    assert (
        is_stale(block_meta, current_hash="aaa", schema_version=1, task_version_id="v2")
        is True
    )


def test_not_stale_when_all_match():
    block_meta = {"cohort_hash": "aaa", "schema_version": 1, "task_version_id": "v1"}
    assert (
        is_stale(block_meta, current_hash="aaa", schema_version=1, task_version_id="v1")
        is False
    )


def test_missing_metadata_is_stale():
    assert (
        is_stale(None, current_hash="aaa", schema_version=1, task_version_id="v1")
        is True
    )


# ---------------------------------------------------------------------------
# get_or_generate_comparison: generation lock
# ---------------------------------------------------------------------------


def _fake_trials(prefix: str) -> list[dict]:
    return [
        {
            "trial_id": f"{prefix}-{i}",
            "components": [],
            "covered_steps": 0,
            "span": 0,
            "coverage": 1.0,
        }
        for i in range(MIN_COHORT)
    ]


@pytest.mark.asyncio
async def test_concurrent_misses_generate_only_once(monkeypatch):
    """Two concurrent requests for the same (task_id, task_version_id) miss
    must not both fire an LLM call. The second call's in-lock recheck should
    observe the first call's freshly written block and skip generation.

    This does not exercise the real AnalyzerBlockModel query -- there is no
    DB in this test process -- so it substitutes ``_load_fresh_comparison``
    with a fake that starts returning a cached row only after generation has
    run once, and substitutes ``AnalyzerBlock.run`` so no real LLM call or S3
    persist happens. What this proves: the lock serializes the two calls and
    the second one's in-lock cache recheck avoids a second generation. It
    does not prove anything about the real SQL query building the correct
    WHERE clause -- that is covered separately by reading the query in
    ``_load_fresh_comparison``.
    """
    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock, AnalyzerOutput

    successful = _fake_trials("s")
    failing = _fake_trials("f")
    generated_output = {
        "schema_version": 1,
        "cohort_success": [t["trial_id"] for t in successful],
        "cohort_failure": [t["trial_id"] for t in failing],
        "categories": [],
    }

    run_calls = 0
    generated_row = {"done": False}

    async def fake_load_fresh(session, *, task_id, task_version_id, current_hash, schema_version):
        if generated_row["done"]:
            return {"cached": True}
        return None

    async def fake_run(self):
        nonlocal run_calls
        run_calls += 1
        # Give the other coroutine a chance to reach the lock first.
        await asyncio.sleep(0.05)
        generated_row["done"] = True
        return AnalyzerOutput(output=generated_output)

    monkeypatch.setattr(
        cc, "resolve_cohorts", AsyncMock(return_value=(successful, failing))
    )
    monkeypatch.setattr(cc, "_load_fresh_comparison", fake_load_fresh)
    monkeypatch.setattr(AnalyzerBlock, "run", fake_run)
    monkeypatch.setattr(
        cc,
        "validate_evidence",
        lambda output, s, f: (output, {"evidence": 0, "observations": 0, "categories": 0}),
    )

    session = MagicMock()

    results = await asyncio.gather(
        cc.get_or_generate_comparison(
            session, "v1", task_id="t1", task_name="task"
        ),
        cc.get_or_generate_comparison(
            session, "v1", task_id="t1", task_name="task"
        ),
    )

    assert run_calls == 1
    assert results[0]["cohort_success"] == generated_output["cohort_success"]
    assert results[1] == {"cached": True}


@pytest.mark.asyncio
async def test_refresh_ignores_a_fresh_block_inside_the_lock(monkeypatch):
    """refresh=True must regenerate even though the in-lock recheck would
    otherwise find a fresh cached row -- mirrors summarize_trajectory's
    documented refresh behaviour."""
    from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock, AnalyzerOutput

    successful = _fake_trials("s")
    failing = _fake_trials("f")
    generated_output = {
        "schema_version": 1,
        "cohort_success": [t["trial_id"] for t in successful],
        "cohort_failure": [t["trial_id"] for t in failing],
        "categories": [],
    }

    load_fresh = AsyncMock(return_value={"cached": True})
    run = AsyncMock(return_value=AnalyzerOutput(output=generated_output))

    monkeypatch.setattr(
        cc, "resolve_cohorts", AsyncMock(return_value=(successful, failing))
    )
    monkeypatch.setattr(cc, "_load_fresh_comparison", load_fresh)
    monkeypatch.setattr(AnalyzerBlock, "run", run)
    monkeypatch.setattr(
        cc,
        "validate_evidence",
        lambda output, s, f: (output, {"evidence": 0, "observations": 0, "categories": 0}),
    )

    session = MagicMock()
    result = await cc.get_or_generate_comparison(
        session, "v1", task_id="t1", task_name="task", refresh=True
    )

    run.assert_awaited_once()
    load_fresh.assert_not_awaited()
    assert result["cohort_success"] == generated_output["cohort_success"]
