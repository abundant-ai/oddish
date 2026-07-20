"""Dashboard queue stats must never fold pipeline counts into a model bucket.

Regression test for an incident where the analysis pipeline was keyed off the
analysis *model*'s queue key: every trial mid-classification surfaced as a
"running" worker under that model's queue on the dashboard (4k+ phantom rows
under one model), and that model's real trial counts were routed into the
"analyses" pipeline. Analysis/verdict counts now live under the reserved
``analysis`` / ``verdict`` buckets regardless of which models the pipelines
run on.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import (  # noqa: E402
    ANALYSIS_PIPELINE_QUEUE_KEY,
    VERDICT_PIPELINE_QUEUE_KEY,
    settings,
)
from oddish.db import AnalysisStatus, VerdictStatus  # noqa: E402
from oddish.queue import (  # noqa: E402
    _assemble_queue_and_pipeline,
    get_queue_stats,
)

ANALYSIS_MODEL_KEY = settings.get_analysis_queue_key()
VERDICT_MODEL_KEY = settings.normalize_queue_key(settings.verdict_model)
# The QA job's concurrency bucket, reported by the reserved pipeline buckets.
# QA leases from the analysis model's bucket, so this coincides with
# ANALYSIS_MODEL_KEY -- keep VERDICT_MODEL_KEY above as the verdict model's own
# key so trials running ON that model still exercise a distinct bucket.
QA_JOB_KEY = settings.get_qa_queue_key()


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _FakeSession:
    """Dispatches the three ``get_queue_stats`` scans off the statement text."""

    async def execute(self, statement, params: dict[str, Any] | None = None):
        sql = str(statement)
        if "FROM trials" in sql and "COALESCE(queue_key, provider)" in sql:
            # Trial rows, including trials running ON the analysis/verdict
            # models themselves -- the collision the reserved keys prevent.
            return _Result(
                [
                    (ANALYSIS_MODEL_KEY, "RUNNING", 7),
                    (ANALYSIS_MODEL_KEY, "QUEUED", 3),
                    (VERDICT_MODEL_KEY, "RUNNING", 5),
                    ("openai/gpt-5.5", "SUCCESS", 11),
                ]
            )
        if "analysis_status" in sql:
            return _Result(
                [
                    (AnalysisStatus.RUNNING, 4099),
                    (AnalysisStatus.SUCCESS, 470935),
                ]
            )
        if "verdict_status" in sql:
            return _Result(
                [
                    (VerdictStatus.QUEUED, 138),
                    (VerdictStatus.RUNNING, 45),
                ]
            )
        raise AssertionError(f"unexpected statement: {sql[:120]}")


@pytest.mark.asyncio
async def test_pipeline_counts_use_reserved_buckets() -> None:
    stats = await get_queue_stats(_FakeSession())

    # Analysis/verdict pipeline counts land in the reserved buckets ...
    assert stats[ANALYSIS_PIPELINE_QUEUE_KEY]["running"] == 4099
    assert stats[ANALYSIS_PIPELINE_QUEUE_KEY]["success"] == 470935
    assert stats[VERDICT_PIPELINE_QUEUE_KEY]["queued"] == 138
    assert stats[VERDICT_PIPELINE_QUEUE_KEY]["running"] == 45

    # ... and the models' own buckets carry ONLY their trial counts.
    assert stats[ANALYSIS_MODEL_KEY] == {
        "pending": 0,
        "queued": 3,
        "running": 7,
        "success": 0,
        "failed": 0,
        "retrying": 0,
        "skipped": 0,
    }
    assert stats[VERDICT_MODEL_KEY]["running"] == 5


@pytest.mark.asyncio
async def test_assemble_routes_model_trials_to_trial_pipeline(monkeypatch) -> None:
    # Give the QA job bucket a concurrency override distinct from every
    # default so the reserved-bucket assertion below cannot pass vacuously
    # (default == default).
    monkeypatch.setitem(settings.model_concurrency_overrides, QA_JOB_KEY, 999)

    stats = await get_queue_stats(_FakeSession())
    queue_stats, pipeline = _assemble_queue_and_pipeline(stats)

    # Trials running on the analysis/verdict models count as trials, not as
    # pipeline work; the pipelines carry exactly the status-column counts.
    assert pipeline["trials"]["running"] == 7 + 5
    assert pipeline["trials"]["success"] == 11
    assert pipeline["analyses"]["running"] == 4099
    assert pipeline["verdicts"]["queued"] == 138

    # The reserved buckets are always present (zero-filled via known keys) and
    # report the QA job bucket's concurrency, not a phantom per-model limit.
    for key in (ANALYSIS_PIPELINE_QUEUE_KEY, VERDICT_PIPELINE_QUEUE_KEY):
        assert key in queue_stats
        assert queue_stats[key]["recommended_concurrency"] == 999
        assert (
            queue_stats[key]["recommended_concurrency"]
            != settings.default_model_concurrency
        )


class _FakeOrgSession:
    """By-org variant of ``_FakeSession`` for ``get_queue_stats_by_org``."""

    async def execute(self, statement, params: dict[str, Any] | None = None):
        sql = str(statement)
        if "FROM trials" in sql and "COALESCE(queue_key, provider)" in sql:
            return _Result([("org-a", ANALYSIS_MODEL_KEY, "RUNNING", 2)])
        if "analysis_status" in sql:
            return _Result([("org-a", AnalysisStatus.RUNNING, 41)])
        if "verdict_status" in sql:
            return _Result([("org-a", VerdictStatus.QUEUED, 17)])
        raise AssertionError(f"unexpected statement: {sql[:120]}")


@pytest.mark.asyncio
async def test_by_org_stats_use_reserved_buckets() -> None:
    from oddish.queue import get_queue_stats_by_org

    stats_by_org = await get_queue_stats_by_org(_FakeOrgSession())

    org_stats = stats_by_org["org-a"]
    assert org_stats[ANALYSIS_PIPELINE_QUEUE_KEY]["running"] == 41
    assert org_stats[VERDICT_PIPELINE_QUEUE_KEY]["queued"] == 17
    # The analysis model's own bucket keeps only its trial counts.
    assert org_stats[ANALYSIS_MODEL_KEY]["running"] == 2


def test_reserved_keys_survive_normalization() -> None:
    # The reserved keys must be fixed points of normalize_queue_key, or the
    # accumulator would fan them back out into inferred provider buckets.
    for key in (ANALYSIS_PIPELINE_QUEUE_KEY, VERDICT_PIPELINE_QUEUE_KEY):
        assert settings.normalize_queue_key(key) == key
    assert ANALYSIS_PIPELINE_QUEUE_KEY in settings.get_known_queue_keys()
    assert VERDICT_PIPELINE_QUEUE_KEY in settings.get_known_queue_keys()
