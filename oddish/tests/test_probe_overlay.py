"""Unit tests for the pure probe-overlay helpers (no DB/S3/filesystem)."""

from types import SimpleNamespace

from oddish.worker.probe_overlay import (
    render_probe_instruction,
    select_related_trials,
)


def _render(has_related: bool) -> str:
    return render_probe_instruction(
        "FRAMING-TEXT",
        "DIRECTIVE-TEXT",
        "ORIGINAL-TEXT",
        related_dir="/app/related_trials",
        has_related=has_related,
    )


def test_render_includes_all_sections():
    out = _render(has_related=True)
    assert "FRAMING-TEXT" in out
    assert "## OPERATOR DIRECTIVE" in out
    assert "DIRECTIVE-TEXT" in out
    assert "## ORIGINAL TASK INSTRUCTION (context only)" in out
    assert "ORIGINAL-TEXT" in out
    assert "## RUNNING TESTS" in out
    assert "## RELATED TRIAL LOGS" in out


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
