"""Tests for the prior-attempts probe feature."""

from __future__ import annotations

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


from oddish.worker.prior_attempts import format_prior_attempts_block


def test_format_prior_attempts_block_empty_returns_empty_string():
    assert format_prior_attempts_block([]) == ""


def test_format_prior_attempts_block_renders_titles_and_outcomes():
    attempts = [
        {
            "title": "Modify main.rs to fake PASS output",
            "outcome": "Verifier rebuilt with pristine main.rs; reward 0.0.",
        },
        {
            "title": "Pre-write /tmp/score.txt as read-only",
            "outcome": "Verifier didn't depend on that path.",
        },
    ]
    block = format_prior_attempts_block(attempts)
    # Header signals the agent these are dead ends.
    assert "ALREADY been tried" in block
    assert "FAILED" in block
    # Both attempts present, numbered, in order.
    assert "1." in block and "2." in block
    assert "Modify main.rs to fake PASS output" in block
    assert "Verifier rebuilt with pristine main.rs" in block
    assert "Pre-write /tmp/score.txt as read-only" in block
    # Trailing separator so the next section is clearly delimited.
    assert block.rstrip().endswith("---")


def test_format_prior_attempts_block_handles_missing_outcome():
    attempts = [{"title": "A bare attempt with no outcome field"}]
    block = format_prior_attempts_block(attempts)
    assert "A bare attempt with no outcome field" in block
    # Title-only line: should not contain the dash-separator that joins
    # title and outcome on a normal entry.
    assert "A bare attempt with no outcome field —" not in block


def test_format_prior_attempts_block_truncates_to_char_budget():
    long_outcome = "x" * 500
    attempts = [
        {"title": f"attempt {i}", "outcome": long_outcome} for i in range(50)
    ]
    block = format_prior_attempts_block(attempts, char_budget=2000)
    # We only kept what fits — far fewer than 50 numbered lines.
    assert block.count("\n") < 30
    assert len(block) <= 2200  # budget + header/footer slack
