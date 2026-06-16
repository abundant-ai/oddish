from api.services.cc_chat.claude_md import render_task_chat_claude_md


def test_task_chat_claude_md_focuses_current_version_and_lists_all():
    out = render_task_chat_claude_md(
        task_name="rust-compiler",
        current_version=2,
        version_trials={2: ["task_1-20"], 1: ["task_1-10"]},
    )
    assert "rust-compiler" in out
    assert "v2" in out and "v1" in out
    assert "task_1-20" in out and "task_1-10" in out
    # current version is called out as the default focus
    assert "current" in out.lower()
