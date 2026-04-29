from oddish.schemas import HarborConfig, TaskSubmission, TrialSpec
from oddish.queue import _build_harbor_config_for_trial


def _make_spec():
    return TrialSpec(agent="claude-code", model="anthropic/claude-sonnet-4-6")


def test_harbor_config_carries_freeform_mode_and_instructions():
    submission = TaskSubmission(
        task_path="some/task",
        trials=[],
        user="alice",
        harbor=HarborConfig(),
        extra_instructions="cheat instructions",
    )
    cfg = _build_harbor_config_for_trial(submission, _make_spec())
    assert cfg is not None
    assert cfg.get("mode") == "freeform"
    assert cfg.get("extra_instructions") == "cheat instructions"


def test_harbor_config_omits_freeform_keys_when_no_extra_instructions():
    submission = TaskSubmission(
        task_path="some/task", trials=[], user="alice", harbor=HarborConfig()
    )
    cfg = _build_harbor_config_for_trial(submission, _make_spec())
    cfg = cfg or {}
    assert cfg.get("mode") != "freeform"
    assert "extra_instructions" not in cfg
