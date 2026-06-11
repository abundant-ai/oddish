from oddish.core.sweeps import build_task_submission_from_sweep, build_trial_specs_from_sweep
from oddish.schemas import TaskSweepSubmission, AgentModelPair


def _sweep(**kw):
    return TaskSweepSubmission(
        task_id="abc",
        configs=[AgentModelPair(agent="nop", model=None, n_trials=1)],
        **kw,
    )


def test_expansion_carries_probe_scope():
    sweep = _sweep(probe_scope="trial", probe_target_trial_id="abc-2",
                   extra_instructions="poke")
    trials = build_trial_specs_from_sweep(sweep)
    sub = build_task_submission_from_sweep(sweep, task_path="abc", trials=trials)
    assert sub.probe_scope == "trial"
    assert sub.probe_target_trial_id == "abc-2"
