from oddish.queue import _build_harbor_config_for_trial
from oddish.schemas import TaskSubmission, TrialSpec


def _sub(**kw):
    return TaskSubmission(
        task_path="t", trials=[TrialSpec(agent="nop", model=None)], **kw
    )


def test_trial_scope_stamped():
    cfg = _build_harbor_config_for_trial(
        _sub(extra_instructions="poke", probe_scope="trial",
             probe_target_trial_id="abc-1"),
        TrialSpec(agent="nop", model=None),
        experiment_id="exp1",
    )
    assert cfg["mode"] == "probe"
    assert cfg["probe_scope"] == "trial"
    assert cfg["probe_target_trial_id"] == "abc-1"


def test_task_scope_stamped_no_target():
    cfg = _build_harbor_config_for_trial(
        _sub(extra_instructions="poke", probe_scope="task"),
        TrialSpec(agent="nop", model=None),
        experiment_id="exp1",
    )
    assert cfg["probe_scope"] == "task"
    assert "probe_target_trial_id" not in cfg
