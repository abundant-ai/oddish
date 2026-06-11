from oddish.schemas import TaskSubmission, TaskSweepSubmission, TrialSpec, AgentModelPair


def test_task_submission_accepts_probe_scope():
    s = TaskSubmission(
        task_path="t",
        trials=[TrialSpec(agent="nop", model=None)],
        probe_scope="trial",
        probe_target_trial_id="abc-0",
    )
    assert s.probe_scope == "trial"
    assert s.probe_target_trial_id == "abc-0"


def test_sweep_submission_accepts_probe_scope():
    s = TaskSweepSubmission(
        task_id="abc",
        configs=[AgentModelPair(agent="nop", model=None, n_trials=1)],
        probe_scope="task",
    )
    assert s.probe_scope == "task"
    assert s.probe_target_trial_id is None
