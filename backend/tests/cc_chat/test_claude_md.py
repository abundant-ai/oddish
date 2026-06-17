from api.services.cc_chat.claude_md import (
    render_experiment_claude_md,
    render_task_chat_claude_md,
    render_task_probes_claude_md,
)


def test_experiment_claude_md_mentions_experiment_id_and_trials():
    out = render_experiment_claude_md(experiment_id="exp_abc", trial_ids=["t1", "t2"])
    assert "exp_abc" in out and "t1" in out and "t2" in out


def test_task_probes_claude_md_mentions_task_name():
    out = render_task_probes_claude_md(task_name="rust-c-compiler", trial_ids=["pr_a"])
    assert "rust-c-compiler" in out and "pr_a" in out


def test_task_chat_claude_md_marks_probes_and_explains_them():
    out = render_task_chat_claude_md(
        task_name="chip8",
        current_version=2,
        version_trials={2: ["t_regular", "t_probe"], 1: ["t_old"]},
        probe_trial_ids={"t_probe"},
    )
    # Probe trial is flagged; regular trials are not.
    assert "`t_probe` (probe)" in out
    assert "`t_regular`" in out and "`t_regular` (probe)" not in out
    # The agent is told probes are a distinct category.
    assert "probe runs" in out.lower()
    assert "extra_instructions" in out
    # Latest version is the default focus.
    assert "v2" in out and "focus here by default" in out


def test_task_chat_claude_md_without_probes_marks_no_trial():
    out = render_task_chat_claude_md(
        task_name="chip8",
        current_version=1,
        version_trials={1: ["t1"]},
    )
    # The explanation mentions "(probe)", but no trial line is tagged.
    assert "`t1` (probe)" not in out
    assert "`t1`" in out
