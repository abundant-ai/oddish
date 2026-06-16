from api.services.cc_chat.claude_md import (
    render_experiment_claude_md,
    render_task_probes_claude_md,
)


def test_experiment_claude_md_mentions_experiment_id_and_trials():
    out = render_experiment_claude_md(experiment_id="exp_abc", trial_ids=["t1", "t2"])
    assert "exp_abc" in out and "t1" in out and "t2" in out


def test_task_probes_claude_md_mentions_task_name():
    out = render_task_probes_claude_md(task_name="rust-c-compiler", trial_ids=["pr_a"])
    assert "rust-c-compiler" in out and "pr_a" in out
