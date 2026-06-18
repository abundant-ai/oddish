from oddish.queue import _build_harbor_config_for_trial
from oddish.schemas import TaskSubmission, TrialSpec


def _spec():
    return TrialSpec(agent="claude-code", model="claude-sonnet-4-6")


def _submission(**kw):
    return TaskSubmission(task_path="/tmp/task", trials=[_spec()], **kw)


def test_task_scope_probe_omits_key():
    cfg = _build_harbor_config_for_trial(
        _submission(extra_instructions="look around"), _spec()
    )
    assert cfg["mode"] == "probe"
    assert "probe_scope" not in cfg


def test_experiment_scope_probe_sets_key():
    cfg = _build_harbor_config_for_trial(
        _submission(extra_instructions="look around", probe_scope="experiment"),
        _spec(),
    )
    assert cfg["probe_scope"] == "experiment"


def test_non_probe_submission_omits_key():
    cfg = _build_harbor_config_for_trial(
        _submission(probe_scope="experiment"), _spec()
    )
    # No extra_instructions => not a probe => no probe_scope leaks in.
    assert cfg is None or "probe_scope" not in cfg
