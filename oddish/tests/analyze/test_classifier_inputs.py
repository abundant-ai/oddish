from oddish.analyze.classifier import build_classify_prompt


def test_placeholders_render_context_when_present():
    prompt = build_classify_prompt(
        result_str="{}", task_dir="/task", trial_dir="/trial",
        trial_agent_context="", pre_trial_context="PRE", file_access_context="FA",
    )
    assert "PRE" in prompt
    assert "FA" in prompt


def test_placeholders_render_none_when_absent():
    prompt = build_classify_prompt(
        result_str="{}", task_dir="/task", trial_dir="/trial",
        trial_agent_context="", pre_trial_context=None, file_access_context=None,
    )
    assert "(none)" in prompt
