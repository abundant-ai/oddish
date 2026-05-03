"""Verify Trial.trajectory_summary mapping is in place."""

from oddish.db.models import TrialModel


def test_trajectory_summary_attribute_is_mapped():
    trial = TrialModel()
    payload = {
        "schema_version": "2",
        "model": "claude-sonnet-4-6",
        "generated_at": "2026-05-02T00:00:00Z",
        "summary": "agent did X then Y",
        "highlights": [{"step_id": 3, "title": "t", "why": "w"}],
    }
    trial.trajectory_summary = payload
    assert trial.trajectory_summary == payload
    assert "trajectory_summary" in TrialModel.__table__.columns


def test_trajectory_summary_defaults_to_none():
    trial = TrialModel()
    assert trial.trajectory_summary is None
