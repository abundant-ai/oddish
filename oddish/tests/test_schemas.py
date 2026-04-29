from oddish.schemas import AgentModelPair, TaskSweepSubmission


def test_task_sweep_submission_accepts_extra_instructions():
    sub = TaskSweepSubmission(
        task_id="task_abc",
        configs=[
            AgentModelPair(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                n_trials=1,
            )
        ],
        user="alice",
        extra_instructions="be adversarial",
    )
    assert sub.extra_instructions == "be adversarial"


def test_task_sweep_submission_extra_instructions_defaults_to_none():
    sub = TaskSweepSubmission(
        task_id="task_abc",
        configs=[
            AgentModelPair(
                agent="claude-code",
                model="anthropic/claude-sonnet-4-6",
                n_trials=1,
            )
        ],
        user="alice",
    )
    assert sub.extra_instructions is None
