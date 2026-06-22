from datetime import datetime

from oddish.core.helpers import build_compact_trial_response, build_trial_response
from oddish.db import TrialOrigin, TrialStatus
from oddish.db.models import TrialModel


def _trial() -> TrialModel:
    # Set the non-nullable columns whose server/Python defaults only apply on
    # flush (not on in-memory construction), so the mapper's TrialResponse
    # validates without a DB round-trip.
    return TrialModel(
        id="t-0",
        name="t-0",
        task_id="task",
        agent="nop",
        provider="nop_oracle",
        queue_key="nop_oracle",
        model=None,
        status=TrialStatus.SUCCESS,
        attempts=1,
        max_attempts=6,
        origin=TrialOrigin.ODDISH,
        is_probe=False,
        has_trajectory=False,
        harbor_sha="e" * 40,
        harbor_config={"source": "https://github.com/dot-agi/harbor"},
        created_at=datetime(2026, 1, 1),
    )


def test_full_mapper_carries_harbor_sha():
    assert build_trial_response(_trial(), task_path="p").harbor_sha == "e" * 40


def test_compact_mapper_carries_harbor_sha():
    assert build_compact_trial_response(_trial(), task_path="p").harbor_sha == "e" * 40


def test_full_mapper_carries_harbor_source():
    resp = build_trial_response(_trial(), task_path="p")
    assert resp.harbor_source == "https://github.com/dot-agi/harbor"


def test_compact_mapper_carries_harbor_source():
    resp = build_compact_trial_response(_trial(), task_path="p")
    assert resp.harbor_source == "https://github.com/dot-agi/harbor"
