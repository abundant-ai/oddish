"""Tests for the prior-attempts probe feature."""

from __future__ import annotations

import pytest

from oddish.queue import _build_harbor_config_for_trial
from oddish.schemas import TaskSubmission, TrialSpec


def _minimal_submission(**overrides) -> TaskSubmission:
    """Helper: build a TaskSubmission with the minimum required fields."""
    base = dict(
        task_path="/tmp/fake-task",
        name="t",
        trials=[TrialSpec(agent="claude-code", model="anthropic/claude-sonnet-4-6")],
        user="alice",
    )
    base.update(overrides)
    return TaskSubmission(**base)


def test_build_harbor_config_persists_preset_name_and_prior_attempts_config():
    submission = _minimal_submission(
        extra_instructions="probe me",
        preset_name="cheat-detector",
        prior_attempts_config={
            "enabled": True,
            "mode": "last_n",
            "last_n": 5,
            "max_attempts": 50,
        },
    )
    result = _build_harbor_config_for_trial(submission, submission.trials[0])
    assert result is not None
    assert result["preset_name"] == "cheat-detector"
    assert result["prior_attempts_config"]["enabled"] is True
    assert result["prior_attempts_config"]["mode"] == "last_n"


def test_build_harbor_config_omits_fields_when_unset():
    submission = _minimal_submission(extra_instructions="probe me")
    result = _build_harbor_config_for_trial(submission, submission.trials[0])
    assert result is not None
    assert "preset_name" not in result
    assert "prior_attempts_config" not in result
