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
    assert "ACCESSING TASK DATA" in out


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


def test_subagents_section_mandates_sequential_dispatch():
    # Subagents share one /app + one verifier, so running a mutator concurrently
    # with a reader makes the reader observe the half-applied exploit as native
    # state. The section must tell the probe to dispatch them one at a time.
    out = _render()
    assert "one at a time" in out.lower()
    assert "wait for each" in out.lower()


def test_subagents_section_mandates_snapshot_restore():
    # Sequential alone leaves residue: a mutating subagent that returns without
    # reverting dirties /app for the next one. Require snapshot-once + restore.
    out = _render()
    # Baseline lives under /probe-harness, NOT /tmp: the verifier's anti-cheat
    # scans /tmp (and /app) for smuggled oracle copies, so a /tmp snapshot that
    # captured a fetched oracle would be flagged and zero the score.
    assert "/probe-harness/app-baseline" in out
    assert "/tmp/app-baseline" not in out
    assert "rsync" in out


def test_subagents_section_mandates_scratch_provenance_tag():
    out = _render()
    assert "probe-scratch-" in out


def test_render_includes_final_summary_section():
    # Operator prompts vary, so the shared overlay (not each prompt) asks for a
    # closing summary shaped to the directive — what the analyzer extracts from.
    out = _render()
    assert "## FINISH WITH A SUMMARY" in out


def test_subagents_section_present_even_without_solution():
    out = render_probe_instruction(
        "FRAMING-TEXT",
        "DIRECTIVE-TEXT",
        "ORIGINAL-TEXT",
        probe_only_paths=[],
    )
    assert "## SUBAGENTS" in out


def test_render_includes_cli_usage_block():
    out = _render()
    assert "ACCESSING TASK DATA" in out
    # concrete, copyable invocations for each capability
    assert "solution cat" in out
    assert "solution fetch" in out
    assert "verifier source" in out
    assert "verify run" in out
    assert "harbor src" in out
    assert "experiments trials" in out
    assert "trials logs" in out
    # defers exhaustive detail to --help
    assert "--help" in out


def test_render_tells_agent_to_use_discretion():
    out = _render()
    low = out.lower()
    assert "discretion" in low or "judgement" in low or "judgment" in low
    # tied to the agent's own directive/prompt
    assert "directive" in low or "your prompt" in low


def test_overlay_has_no_stage_or_harbor_path_constants():
    import oddish.worker.probe_overlay as ov
    for name in ("STAGE_DIR", "STAGE_DIR_ENV", "BOUNDARY_MARKER_NAME",
                 "BOUNDARY_MARKER_TEXT", "HARBOR_DIR_NAME", "HARBOR_CONTAINER_DIR"):
        assert not hasattr(ov, name), f"{name} should be removed"
