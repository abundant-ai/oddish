"""Unit tests for the pure probe-overlay helpers (no DB/S3/filesystem)."""

from types import SimpleNamespace

from oddish.worker.probe_overlay import (
    render_probe_instruction,
    select_related_trials,
)


def _render(has_related: bool, has_task_files: bool = False) -> str:
    return render_probe_instruction(
        "FRAMING-TEXT",
        "DIRECTIVE-TEXT",
        "ORIGINAL-TEXT",
        related_dir="/app/related_trials",
        has_related=has_related,
        has_task_files=has_task_files,
    )


def test_render_includes_all_sections():
    # Inline natural-flow layout: directive flows straight into the task spec
    # (no "OPERATOR DIRECTIVE"/"ORIGINAL TASK" headers, framing intentionally
    # unused) -- see render_probe_instruction.
    out = _render(has_related=True)
    assert "DIRECTIVE-TEXT" in out
    assert "THIS IS THE TASK:" in out
    assert "ORIGINAL-TEXT" in out
    assert "## RUNNING TESTS" in out
    assert "## RELATED TRIAL LOGS" in out


def test_render_task_files_section_present_when_staged():
    out = _render(has_related=True, has_task_files=True)
    assert "## FULL TASK DEFINITION" in out
    assert "task_files/" in out
    # Frames the normal-run baseline so the agent grasps it has extra surface.
    assert "Normally an agent only sees" in out


def test_render_task_files_section_absent_by_default():
    out = _render(has_related=True)
    assert "FULL TASK DEFINITION" not in out
    assert "task_files/" not in out


def test_render_running_tests_is_fuzzy():
    out = _render(has_related=True)
    # Mentions multiple possible verifier names rather than one hard path.
    assert "run_tests.sh" in out
    assert "tests/test.sh" in out


def test_render_related_section_present_when_staged():
    out = _render(has_related=True)
    assert "/app/related_trials" in out
    assert "read-only" in out


def test_render_related_section_softened_when_none():
    out = _render(has_related=False)
    assert "No prior non-probe attempts were available" in out
    # Should not claim a staged directory exists.
    assert "/app/related_trials" not in out


def test_select_excludes_current_and_probes():
    trials = [
        SimpleNamespace(id="t-0", harbor_config=None),  # real attempt
        SimpleNamespace(id="t-1", harbor_config={"mode": "probe"}),  # probe
        SimpleNamespace(id="t-2", harbor_config={"extra_instructions": "x"}),  # real
        SimpleNamespace(id="t-3", harbor_config={"mode": "probe"}),  # current (also probe)
    ]
    selected = select_related_trials(trials, current_trial_id="t-3")
    ids = [t.id for t in selected]
    assert ids == ["t-0", "t-2"]


def test_select_empty_when_only_probes_and_self():
    trials = [
        SimpleNamespace(id="me", harbor_config={"mode": "probe"}),
        SimpleNamespace(id="other-probe", harbor_config={"mode": "probe"}),
    ]
    assert select_related_trials(trials, current_trial_id="me") == []
