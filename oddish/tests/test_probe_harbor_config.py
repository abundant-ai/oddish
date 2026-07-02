from oddish.schemas import HarborConfig, TaskSubmission, TrialSpec
from oddish.queue import _build_harbor_config_for_trial


def _compute_trial_environment(spec, harbor_config):
    """Mirror the queue.py expression that defaults probes to 'modal'."""
    return spec.environment or (
        "modal" if (harbor_config or {}).get("mode") == "probe" else None
    )


def _make_spec():
    return TrialSpec(agent="claude-code", model="anthropic/claude-sonnet-4-6")


def test_harbor_config_carries_probe_mode_and_instructions():
    submission = TaskSubmission(
        task_path="some/task",
        trials=[],
        user="alice",
        harbor=HarborConfig(),
        extra_instructions="cheat instructions",
    )
    cfg = _build_harbor_config_for_trial(submission, _make_spec())
    assert cfg is not None
    assert cfg.get("mode") == "probe"
    assert cfg.get("extra_instructions") == "cheat instructions"


def test_harbor_config_omits_probe_keys_when_no_extra_instructions():
    submission = TaskSubmission(
        task_path="some/task", trials=[], user="alice", harbor=HarborConfig()
    )
    cfg = _build_harbor_config_for_trial(submission, _make_spec())
    cfg = cfg or {}
    assert cfg.get("mode") != "probe"
    assert "extra_instructions" not in cfg


def test_harbor_config_carries_result_focus():
    submission = TaskSubmission(
        task_path="some/task",
        trials=[],
        user="alice",
        harbor=HarborConfig(),
        extra_instructions="cheat",
        result_focus="Did the agent find ambiguities?",
    )
    cfg = _build_harbor_config_for_trial(submission, _make_spec())
    assert cfg["result_focus"] == "Did the agent find ambiguities?"


def test_probe_trial_defaults_to_modal_environment():
    submission = TaskSubmission(
        task_path="some/task",
        trials=[],
        user="alice",
        harbor=HarborConfig(),
        extra_instructions="cheat instructions",
    )
    spec = _make_spec()  # spec.environment is None by default
    harbor_config = _build_harbor_config_for_trial(submission, spec)
    assert _compute_trial_environment(spec, harbor_config) == "modal"


def test_non_probe_trial_environment_unchanged():
    submission = TaskSubmission(
        task_path="some/task",
        trials=[],
        user="alice",
        harbor=HarborConfig(),
    )
    spec = _make_spec()
    harbor_config = _build_harbor_config_for_trial(submission, spec)
    assert _compute_trial_environment(spec, harbor_config) is None
