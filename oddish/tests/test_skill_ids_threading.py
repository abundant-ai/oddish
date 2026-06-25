from oddish.queue import _build_harbor_config_for_trial
from oddish.schemas import TaskSubmission, TrialSpec


def _submission(**kw):
    return TaskSubmission(
        task_path="/tmp/task",
        trials=[TrialSpec(agent="claude-code", model="anthropic/claude-sonnet-4-6")],
        **kw,
    )


def test_skill_ids_stored_in_harbor_config():
    sub = _submission(extra_instructions="probe it", skill_ids=["s1", "s2"])
    cfg = _build_harbor_config_for_trial(sub, sub.trials[0])
    assert cfg["skill_ids"] == ["s1", "s2"]


def test_no_skill_ids_key_when_empty():
    sub = _submission(extra_instructions="probe it")
    cfg = _build_harbor_config_for_trial(sub, sub.trials[0])
    assert "skill_ids" not in cfg
