"""Unit tests for the pure probe-overlay helpers (no DB/S3/filesystem)."""

from types import SimpleNamespace

from oddish.worker.probe_overlay import (
    AGENT_BRIEF_CONTAINER_PATH,
    MAX_VISIBILITY_FILES,
    render_probe_instruction,
    render_visibility_map,
    select_related_trials,
)


def _render(has_related: bool) -> str:
    return render_probe_instruction(
        "FRAMING-TEXT",
        "DIRECTIVE-TEXT",
        "ORIGINAL-TEXT",
        related_dir="/app/related_trials",
        has_related=has_related,
        env_files=["src/app.py"],
        probe_only_paths=["tests/", "solution/"],
    )


def test_render_includes_all_sections():
    out = _render(has_related=True)
    assert "DIRECTIVE-TEXT" in out
    # The original spec is relabeled as the real agent's brief, not the probe's.
    assert "REAL AGENT BRIEF" in out
    assert "ORIGINAL-TEXT" in out
    assert "## WHAT THE REAL AGENT SEES vs WHAT YOU SEE" in out
    assert "## RUNNING TESTS" in out
    assert "## RELATED TRIAL LOGS" in out


def test_render_does_not_label_spec_as_probes_own_task():
    # Regression: the old "THIS IS THE TASK:" framing made the probe adopt the
    # solving-agent persona and flag its own staged files as vulnerabilities.
    out = _render(has_related=True)
    assert "THIS IS THE TASK" not in out
    assert AGENT_BRIEF_CONTAINER_PATH in out


def test_visibility_map_lists_env_and_probe_only():
    out = render_visibility_map(
        env_files=["src/app.py", "Dockerfile"],
        probe_only_paths=["tests/", "solution/", "harbor_src/"],
    )
    assert "environment/src/app.py" in out
    assert "environment/Dockerfile" in out
    for p in ("tests/", "solution/", "harbor_src/"):
        assert p in out
    # Rule 1: access to a probe-only file is not a vuln.
    assert "unless the same file" in out
    # Rule 2: a hidden answer key in a probe-only file is by design, not a leak.
    assert "by design" in out
    assert "leak" in out.lower()


def test_visibility_map_truncates_long_env_listing():
    files = [f"f{i}.py" for i in range(MAX_VISIBILITY_FILES + 25)]
    out = render_visibility_map(env_files=files, probe_only_paths=[])
    assert "25 more files under environment/" in out


def test_visibility_map_handles_no_environment():
    out = render_visibility_map(env_files=[], probe_only_paths=["tests/"])
    assert "no `environment/` directory" in out


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
