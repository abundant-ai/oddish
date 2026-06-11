import pytest
from fastapi import HTTPException

from oddish.core.sweeps import validate_sweep_submission
from oddish.schemas import TaskSweepSubmission, AgentModelPair


def _sweep(**kw):
    return TaskSweepSubmission(
        task_id="abc",
        configs=[AgentModelPair(agent="nop", model=None, n_trials=1)],
        **kw,
    )


def test_trial_scope_requires_target():
    with pytest.raises(HTTPException):
        validate_sweep_submission(_sweep(probe_scope="trial"))


def test_trial_scope_target_must_belong_to_task():
    with pytest.raises(HTTPException):
        validate_sweep_submission(
            _sweep(probe_scope="trial", probe_target_trial_id="other-0")
        )


def test_task_scope_forbids_target():
    with pytest.raises(HTTPException):
        validate_sweep_submission(
            _sweep(probe_scope="task", probe_target_trial_id="abc-0")
        )


def test_valid_trial_scope_passes():
    validate_sweep_submission(
        _sweep(probe_scope="trial", probe_target_trial_id="abc-0")
    )


def test_legacy_extra_instructions_defaults_to_task():
    s = _sweep(extra_instructions="poke")
    validate_sweep_submission(s)
    assert s.probe_scope == "task"
