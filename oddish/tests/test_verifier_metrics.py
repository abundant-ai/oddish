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


def test_invalid_candidate_does_not_mask_later_valid_metrics(tmp_path):
    # One malformed metrics.json (e.g. a stray nested copy) must not hide a
    # valid one elsewhere in the tree.
    bad_dir = tmp_path / "a-trial__x" / "verifier"
    bad_dir.mkdir(parents=True)
    (bad_dir / "metrics.json").write_text("{broken")
    good = {"schema_version": 1, "device_count": 4}
    good_dir = tmp_path / "b-trial__y" / "verifier"
    good_dir.mkdir(parents=True)
    good_dir.joinpath("metrics.json").write_text(json.dumps(good))
    assert extract_verifier_metrics(tmp_path) == good


def test_merged_result_passthrough_without_exception():
    from oddish.workers.harbor.outcome import merged_trial_result

    assert merged_trial_result({"a": 1}, None, None) == {"a": 1}
    assert merged_trial_result(None, None, None) is None


def test_merged_result_marks_quiet_exception():
    from oddish.workers.harbor.outcome import merged_trial_result

    merged = merged_trial_result(
        {"gates": {}, "failure_reason": "port.py missing"},
        "Command failed (exit 1): gemini --model=bad",
        "NonZeroAgentExitCodeError",
    )
    assert merged["gates"] == {}
    assert merged["harbor_exception"]["exception_type"] == "NonZeroAgentExitCodeError"
    assert "exit 1" in merged["harbor_exception"]["error"]


def test_merged_result_marker_only_when_no_metrics():
    from oddish.workers.harbor.outcome import merged_trial_result

    merged = merged_trial_result(None, "boom", "AgentSetupTimeoutError")
    assert merged == {
        "harbor_exception": {
            "exception_type": "AgentSetupTimeoutError",
            "error": "boom",
        }
    }


def test_merged_result_truncates_long_errors():
    from oddish.workers.harbor.outcome import merged_trial_result

    merged = merged_trial_result(None, "x" * 1000, "SomeError")
    assert len(merged["harbor_exception"]["error"]) == 300


def test_nonfinite_metrics_rejected(tmp_path):
    vdir = tmp_path / "verifier"
    vdir.mkdir()
    (vdir / "metrics.json").write_text('{"tokens_per_sec": NaN}')
    # NaN would parse under stock json but break JSONB persistence; the
    # extractor must skip it rather than return an unpersistable dict.
    assert extract_verifier_metrics(tmp_path) is None
