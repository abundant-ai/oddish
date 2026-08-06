"""The trial sidebar shows the audit of the version THAT trial ran on.

A task re-uploaded after its audit has findings describing a snapshot the trial
never saw; attaching those to the trial would misattribute them.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.endpoints.trials import _attach_pre_trial_audit  # noqa: E402
from oddish.db.models import TaskVersionModel, VerdictStatus  # noqa: E402
from oddish.schemas import TrialResponse  # noqa: E402

_FINDING = {"tier": "must_fix", "title": "Verifier ignores stderr"}


def _response() -> TrialResponse:
    return TrialResponse(
        id="trial_1",
        name="trial_1",
        task_id="task_1",
        task_path="/tmp/task",
        agent="claude-code",
        provider="anthropic",
        queue_key="anthropic/claude-opus-5",
        model=None,
        status="success",
        attempts=1,
        max_attempts=3,
        harbor_stage=None,
        error_message=None,
        result=None,
        created_at=datetime(2026, 7, 28, tzinfo=timezone.utc),
        started_at=None,
        finished_at=None,
    )


class _Session:
    def __init__(self, versions: dict):
        self._versions = versions
        self.asked_for: list[str] = []

    async def get(self, model, row_id, **kw):
        assert model is TaskVersionModel
        self.asked_for.append(row_id)
        return self._versions.get(row_id)


@pytest.mark.asyncio
async def test_attaches_the_audit_of_the_trials_own_version():
    audited = SimpleNamespace(
        pre_trial={"items": [_FINDING]},
        pre_trial_status=VerdictStatus.SUCCESS,
        pre_trial_error=None,
    )
    session = _Session({"task_1-v1": audited})

    out = await _attach_pre_trial_audit(session, _response(), "task_1-v1")

    assert session.asked_for == ["task_1-v1"]
    assert out.pre_trial_status == "success"
    assert [f["title"] for f in out.pre_trial_findings] == ["Verifier ignores stderr"]


@pytest.mark.asyncio
async def test_a_newer_unaudited_version_shows_nothing():
    """v5 was never audited; v1's findings must not leak onto a v5 trial."""
    session = _Session(
        {
            "task_1-v1": SimpleNamespace(
                pre_trial={"items": [_FINDING]},
                pre_trial_status=VerdictStatus.SUCCESS,
                pre_trial_error=None,
            ),
            "task_1-v5": SimpleNamespace(
                pre_trial=None, pre_trial_status=None, pre_trial_error=None
            ),
        }
    )

    out = await _attach_pre_trial_audit(session, _response(), "task_1-v5")

    assert out.pre_trial_findings == []
    assert out.pre_trial_status is None


@pytest.mark.asyncio
async def test_failed_audit_keeps_its_error_and_no_findings():
    session = _Session(
        {
            "task_1-v1": SimpleNamespace(
                pre_trial=None,
                pre_trial_status=VerdictStatus.FAILED,
                pre_trial_error="TimeoutError()",
            )
        }
    )

    out = await _attach_pre_trial_audit(session, _response(), "task_1-v1")

    assert out.pre_trial_status == "failed"
    assert out.pre_trial_error == "TimeoutError()"
    assert out.pre_trial_findings == []


@pytest.mark.asyncio
async def test_no_version_pin_skips_the_lookup_entirely():
    session = _Session({})
    out = await _attach_pre_trial_audit(session, _response(), None)
    assert session.asked_for == []
    assert out.pre_trial_findings == []
