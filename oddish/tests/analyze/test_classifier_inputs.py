import json

from oddish.analyze.classifier import _write_qa_context, build_classify_prompt


def test_placeholders_render_context_when_present():
    prompt = build_classify_prompt(
        result_str="{}", task_dir="/task", trial_dir="/trial",
        trial_agent_context="", pre_trial_context="PRE", file_access_context="FA",
        trajectory_components_context="TRAJ",
    )
    assert "PRE" in prompt
    assert "FA" in prompt
    assert "TRAJ" in prompt


def test_placeholders_render_none_when_absent():
    prompt = build_classify_prompt(
        result_str="{}", task_dir="/task", trial_dir="/trial",
        trial_agent_context="", pre_trial_context=None, file_access_context=None,
        trajectory_components_context=None,
    )
    assert "(none)" in prompt


def test_pre_trial_must_fix_requires_causality_to_change_trial_label():
    prompt = build_classify_prompt(
        result_str="fail",
        task_dir="/task",
        trial_dir="/trial",
        trial_agent_context="",
        pre_trial_context="one must_fix finding",
    )

    assert "does not change the trial label by itself" in prompt
    assert (
        "When the agent failed for an independent reason, use GOOD_FAILURE" in prompt
    )
    assert "the task verdict evaluates latent defects separately" in prompt


def test_write_qa_context_writes_trajectory_components(tmp_path):
    components = [
        {"step_ids": [0, 1], "trajectory_component": "debugging", "summary": "s",
         "tool_count": 3, "duration_ms": 1200},
    ]
    qa_dir, pre_ctx, fa_ctx, traj_ctx = _write_qa_context(
        tmp_path, pre_trial_items=None, file_access=None,
        trajectory_components=components,
    )

    written = tmp_path / ".qa_context" / "trajectory_components.json"
    assert written.exists()
    assert json.loads(written.read_text()) == components
    assert traj_ctx is not None
    assert "trajectory_components.json" in traj_ctx
    assert pre_ctx is None and fa_ctx is None


def test_write_qa_context_absent_trajectory_components(tmp_path):
    qa_dir, pre_ctx, fa_ctx, traj_ctx = _write_qa_context(
        tmp_path, pre_trial_items=None, file_access=None, trajectory_components=None,
    )
    assert qa_dir is None
    assert traj_ctx is None
    assert not (tmp_path / ".qa_context" / "trajectory_components.json").exists()
