"""The task detail bundle must carry pre-trial findings per version.

`task_versions.pre_trial` is where both audit paths land their findings, but the
API never exposed it, so the task page had nothing to render.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.task_detail import (  # noqa: E402
    _aggregate_task_detail_rollups,
)
from oddish.db.models import VerdictStatus  # noqa: E402
from oddish.schemas import TaskVersionResponse  # noqa: E402

_FINDING = {
    "id": "abc123",
    "source": "pre_trial",
    "dimension": "verifier",
    "problem_type": "incompleteness",
    "tier": "must_fix",
    "file": "tests/verify.py",
    "line_start": 10,
    "line_end": 12,
    "title": "Verifier ignores stderr",
    "detail": "Only stdout is asserted.",
    "recommendation": "Assert stderr too.",
}


def _version(**kw):
    base = dict(
        id="v1",
        task_id="task_1",
        version=1,
        task_path="/tmp/task",
        created_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )
    base.update(kw)
    return TaskVersionResponse(**base)


def test_task_version_response_carries_pre_trial_from_the_orm_row():
    row = SimpleNamespace(
        id="v1",
        task_id="task_1",
        version=1,
        task_path="/tmp/task",
        task_s3_key=None,
        content_hash=None,
        message=None,
        created_by_user_id=None,
        created_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        pre_trial={"items": [_FINDING]},
        pre_trial_status=VerdictStatus.SUCCESS,
    )
    resp = TaskVersionResponse.model_validate(row)
    assert resp.pre_trial == {"items": [_FINDING]}
    # JobStatus values are lowercase on the wire.
    assert resp.pre_trial_status == "success"


def test_version_summary_exposes_findings_for_the_task_page():
    _totals, versions = _aggregate_task_detail_rollups(
        trials=[],
        version_rows=[_version(pre_trial={"items": [_FINDING]},
                               pre_trial_status=VerdictStatus.SUCCESS)],
        current_version_id="v1",
    )
    assert len(versions) == 1
    assert versions[0].pre_trial_status == "success"
    assert [f["title"] for f in versions[0].pre_trial_findings] == [
        "Verifier ignores stderr"
    ]


def test_version_without_an_audit_reports_no_findings():
    _totals, versions = _aggregate_task_detail_rollups(
        trials=[], version_rows=[_version()], current_version_id="v1"
    )
    # Distinguishable from "audited, found nothing" (status "success").
    assert versions[0].pre_trial_findings == []
    assert versions[0].pre_trial_status is None


def test_failed_audit_carries_its_error_and_no_findings():
    """A failed audit must not look like a clean one.

    `sync_pre_trial_to_task_version` clears `pre_trial` when it records a
    failure, so findings are empty for both outcomes -- only the status and
    error distinguish them.
    """
    _totals, versions = _aggregate_task_detail_rollups(
        trials=[],
        version_rows=[
            _version(
                pre_trial=None,
                pre_trial_status=VerdictStatus.FAILED,
                pre_trial_error="TimeoutError()",
            )
        ],
        current_version_id="v1",
    )
    assert versions[0].pre_trial_findings == []
    assert versions[0].pre_trial_status == "failed"
    assert versions[0].pre_trial_error == "TimeoutError()"


def test_in_flight_audit_reports_running():
    """The claim sets RUNNING before any agent work, so a version can sit in
    `running` with no findings for the length of the audit."""
    _totals, versions = _aggregate_task_detail_rollups(
        trials=[],
        version_rows=[_version(pre_trial=None, pre_trial_status=VerdictStatus.RUNNING)],
        current_version_id="v1",
    )
    assert versions[0].pre_trial_findings == []
    assert versions[0].pre_trial_status == "running"
    assert versions[0].pre_trial_error is None


def test_audit_cost_rides_on_the_version_payload():
    """`analysis_costs` has no version reference, so the audit's spend is stored
    in the payload at write time and read straight back out."""
    _totals, versions = _aggregate_task_detail_rollups(
        trials=[],
        version_rows=[
            _version(
                pre_trial={"items": [_FINDING], "cost_usd": 0.146, "block_id": "b1"},
                pre_trial_status=VerdictStatus.SUCCESS,
            )
        ],
        current_version_id="v1",
    )
    assert versions[0].pre_trial_cost_usd == 0.146


def test_audits_predating_cost_capture_report_none():
    _totals, versions = _aggregate_task_detail_rollups(
        trials=[],
        version_rows=[
            _version(
                pre_trial={"items": [_FINDING]},
                pre_trial_status=VerdictStatus.SUCCESS,
            )
        ],
        current_version_id="v1",
    )
    # Absent, not zero: zero would claim the audit was free.
    assert versions[0].pre_trial_cost_usd is None
