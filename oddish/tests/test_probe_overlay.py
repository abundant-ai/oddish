"""Unit tests for the pure probe-overlay helpers (no DB/S3/filesystem)."""

from oddish.worker.probe_overlay import (
    PROBE_HARNESS_DIR,
    QUERY_CLI_CONTAINER_PATH,
    render_probe_instruction,
    render_visibility_map,
)


def _render() -> str:
    return render_probe_instruction(
        "FRAMING-TEXT",
        "DIRECTIVE-TEXT",
        "ORIGINAL-TEXT",
        probe_only_paths=["tests/", "solution/"],
    )


def test_render_includes_all_sections():
    out = _render()
    assert "DIRECTIVE-TEXT" in out
    # The original spec is relabeled as the real agent's brief, not the probe's.
    assert "REAL AGENT BRIEF" in out
    assert "ORIGINAL-TEXT" in out
    assert "## WHAT THE REAL AGENT SEES vs WHAT YOU SEE" in out
    assert "## RUNNING THE VERIFIER" in out
    assert "## TRIAL DATA" in out


def test_render_does_not_label_spec_as_probes_own_task():
    # Regression: the old "THIS IS THE TASK:" framing made the probe adopt the
    # solving-agent persona and flag its own staged files as vulnerabilities.
    out = _render()
    assert "THIS IS THE TASK" not in out


def test_visibility_map_is_cli_centric():
    out = render_visibility_map(["tests/", "solution/", "harbor_src/"])
    assert "## WHAT THE REAL AGENT SEES vs WHAT YOU SEE" in out
    assert "/app" in out
    # No longer enumerates probe-only files as filesystem paths the agent could read.
    assert f"{PROBE_HARNESS_DIR}/solution/" not in out
    # Points at the CLI as the one channel for probe-only material.
    assert "oddish-query" in out
    # Rules preserved.
    assert "does not exist in a real run" in out
    assert "by design" in out
    assert "leak" in out.lower()


def test_visibility_map_handles_empty_harness():
    out = render_visibility_map([])
    assert "oddish-query" in out
    assert "by design" in out


def test_running_tests_points_at_verify_cli():
    out = _render()
    assert "verify run" in out
    assert "oddish-query" in out


def test_render_cli_section_present():
    out = _render()
    assert QUERY_CLI_CONTAINER_PATH in out
    assert "node /probe-harness/oddish-query" in out
    assert "RELATED TRIAL LOGS" not in out


def test_render_includes_oracle_seed_when_solution_staged():
    out = _render()
    assert "## REFERENCE SOLUTION" in out
    # Seeds via the CLI now, not a mounted path.
    assert "solution fetch" in out
    assert "head start" in out.lower()


def test_render_omits_oracle_seed_when_no_solution_staged():
    out = render_probe_instruction(
        "FRAMING-TEXT",
        "DIRECTIVE-TEXT",
        "ORIGINAL-TEXT",
        probe_only_paths=["tests/", "harbor_src/"],
    )
    assert "## REFERENCE SOLUTION" not in out


def test_render_includes_subagents_section():
    out = _render()
    assert "## SUBAGENTS" in out
    # States the one-level nesting limit so the probe fans out directly.
    assert "one level" in out.lower()


def test_subagents_section_present_even_without_solution():
    out = render_probe_instruction(
        "FRAMING-TEXT",
        "DIRECTIVE-TEXT",
        "ORIGINAL-TEXT",
        probe_only_paths=[],
    )
    assert "## SUBAGENTS" in out
