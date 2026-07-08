"""The verifier metrics.json contract: benchmark tasks report structured
metrics by writing ``verifier/metrics.json``; the worker persists it onto the
trial as its structured result.

Extraction is deliberately forgiving: a missing, malformed, oversized, or
non-object file yields None (the trial's reward is never at stake), so a task
cannot fail merely because its metrics emission broke.
"""

from __future__ import annotations

import json
from pathlib import Path

from oddish.core.harbor_artifacts import (
    VERIFIER_METRICS_MAX_BYTES,
    extract_verifier_metrics,
)
from oddish.workers.harbor.outcome import HarborOutcome


def _write_metrics(job_dir: Path, payload: str) -> Path:
    verifier_dir = job_dir / "tpu-task__abc123" / "verifier"
    verifier_dir.mkdir(parents=True)
    p = verifier_dir / "metrics.json"
    p.write_text(payload)
    return p


def test_metrics_json_extracted_as_dict(tmp_path):
    metrics = {
        "schema_version": 1,
        "ttft_ms": 12.5,
        "throughput_tokens_per_sec": 4300,
        "mxu_utilization_pct": 41.2,
    }
    _write_metrics(tmp_path, json.dumps(metrics))
    assert extract_verifier_metrics(tmp_path) == metrics


def test_missing_metrics_json_is_none(tmp_path):
    (tmp_path / "tpu-task__abc123" / "verifier").mkdir(parents=True)
    assert extract_verifier_metrics(tmp_path) is None


def test_invalid_json_is_none(tmp_path):
    _write_metrics(tmp_path, "{not json")
    assert extract_verifier_metrics(tmp_path) is None


def test_non_object_json_is_none(tmp_path):
    # The contract is a JSON OBJECT; a bare list/scalar has no keys to display.
    _write_metrics(tmp_path, json.dumps([1, 2, 3]))
    assert extract_verifier_metrics(tmp_path) is None


def test_oversized_metrics_json_is_none(tmp_path):
    big = json.dumps({"blob": "x" * (VERIFIER_METRICS_MAX_BYTES + 1)})
    _write_metrics(tmp_path, big)
    assert extract_verifier_metrics(tmp_path) is None


def test_outcome_carries_metrics_field_defaulting_none():
    outcome = HarborOutcome(
        reward=1.0,
        error=None,
        exit_code=0,
        duration_sec=1.0,
        job_result_path=None,
        job_dir=None,
    )
    assert outcome.metrics is None
