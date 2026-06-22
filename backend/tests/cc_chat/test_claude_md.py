from api.services.cc_chat.claude_md import (
    render_experiment_claude_md,
    render_task_chat_claude_md,
    render_task_probes_claude_md,
    render_global_claude_md,
)


def test_experiment_template_uses_cli():
    md = render_experiment_claude_md(experiment_id="exp1")
    assert "oddish-query experiments trials exp1" in md
    assert "jobs/" not in md  # no mounted-file layout


def test_task_and_probes_use_cli():
    assert "./oddish-query tasks search" in render_task_chat_claude_md(task_name="t")
    assert "./oddish-query tasks search" in render_task_probes_claude_md(task_name="t")


def test_global_lists_experiments_command():
    assert "experiments trials" in render_global_claude_md(org_id="o")


def test_global_claude_md_documents_cli_and_discipline():
    md = render_global_claude_md(org_id="org_42")
    assert "oddish-query tasks search" in md
    assert "oddish-query trials logs" in md
    assert "search" in md.lower()
    assert "one trial at a time" in md.lower()


def test_experiment_template_bakes_in_id():
    md = render_experiment_claude_md(experiment_id="exp_abc")
    assert "exp_abc" in md


def test_task_probes_template_bakes_in_task_name():
    md = render_task_probes_claude_md(task_name="rust-c-compiler")
    assert "rust-c-compiler" in md


def test_task_chat_template_bakes_in_task_name():
    md = render_task_chat_claude_md(task_name="chip8")
    assert "chip8" in md


def test_no_jobs_mount_prose_in_any_template():
    for fn, kwargs in [
        (render_experiment_claude_md, {"experiment_id": "e1"}),
        (render_task_chat_claude_md, {"task_name": "t1"}),
        (render_task_probes_claude_md, {"task_name": "t1"}),
        (render_global_claude_md, {"org_id": "o1"}),
    ]:
        assert "jobs/" not in fn(**kwargs)
