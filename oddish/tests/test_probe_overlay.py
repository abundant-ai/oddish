"""Unit tests for the pure probe-overlay helpers (no DB/S3/filesystem)."""

from oddish.worker.probe_overlay import (
    AGENT_BRIEF_CONTAINER_PATH,
    PROBE_HARNESS_DIR,
    QUERY_CLI_CONTAINER_PATH,
    render_probe_instruction,
    render_visibility_map,
)


def _render(result_focus: str | None = None) -> str:
    return render_probe_instruction(
        "FRAMING-TEXT",
        "DIRECTIVE-TEXT",
        "ORIGINAL-TEXT",
        probe_only_paths=["tests/", "solution/"],
        result_focus=result_focus,
    )


def test_render_includes_all_sections():
    out = _render()
    assert "DIRECTIVE-TEXT" in out
    # The original spec is relabeled as the real agent's brief, not the probe's.
    assert "REAL AGENT BRIEF" in out
    assert "ORIGINAL-TEXT" in out
    assert "## WHAT THE REAL AGENT SEES vs WHAT YOU SEE" in out
    assert "## RUNNING TESTS" in out
    assert "## TRIAL DATA" in out
    assert "## ACTION ITEMS" in out


def test_trial_data_section_asks_to_check_audited_task_logs():
    out = _render()
    # The probe is told to check the audited task's prior trial logs up front.
    assert "task you're auditing" in out
    assert "tasks search" in out
    assert "tasks trials <task_id>" in out


def test_trial_data_section_documents_all_cli_commands():
    out = _render()
    # Every oddish-query subcommand is documented, including `tasks get`.
    for cmd in ("tasks search", "tasks get", "tasks trials",
                "experiments trials", "trials logs"):
        assert cmd in out


def test_trial_data_section_gives_runnable_examples():
    out = _render()
    # Copy-pasteable invocations with the real `node <cli>` prefix.
    assert f"node {QUERY_CLI_CONTAINER_PATH} tasks search" in out
    assert f"node {QUERY_CLI_CONTAINER_PATH} tasks get <task_id>" in out
    assert f"node {QUERY_CLI_CONTAINER_PATH} trials logs <trial_id> --trajectory" in out


def test_action_items_section_demands_verified_high_quality_items():
    out = _render()
    # Deliverable: high-quality action items, verified before reporting.
    assert "high-quality" in out
    assert "VERIFY before you report" in out


def test_json_output_replaces_action_items_when_schema_provided():
    schema = '{"type": "object", "properties": {"verdict": {"type": "string"}}}'
    out = _render(result_focus=schema)
    # The JSON output deliverable replaces the free-form action items section.
    assert "## OUTPUT — required JSON" in out
    assert "## ACTION ITEMS" not in out
    # The operator's schema is rendered verbatim in a json code block.
    assert '"verdict"' in out
    assert "```json" in out


def test_plain_text_result_focus_keeps_action_items():
    # A prose focus question is NOT a JSON object, so action items stay.
    out = _render(result_focus="Did the agent attempt reward hacking?")
    assert "## ACTION ITEMS" in out
    assert "## OUTPUT — required JSON" not in out


def test_render_does_not_label_spec_as_probes_own_task():
    # Regression: the old "THIS IS THE TASK:" framing made the probe adopt the
    # solving-agent persona and flag its own staged files as vulnerabilities.
    out = _render()
    assert "THIS IS THE TASK" not in out
    assert AGENT_BRIEF_CONTAINER_PATH in out


def test_visibility_map_separates_app_from_harness():
    out = render_visibility_map(["tests/", "solution/", "harbor_src/"])
    # /app is framed as the real agent's pristine workspace.
    assert "/app" in out
    # Probe-only material is enumerated under the harness root, not /app.
    for p in ("tests/", "solution/", "harbor_src/"):
        assert f"{PROBE_HARNESS_DIR}/{p}" in out
    # Rule 1: access to a harness file is not an agent-reachable vuln.
    assert "does not exist in a real run" in out
    # Rule 2: a hidden answer key under the harness root is by design, not a leak.
    assert "by design" in out
    assert "leak" in out.lower()


def test_visibility_map_handles_empty_harness():
    out = render_visibility_map([])
    assert PROBE_HARNESS_DIR in out
    # Still states the two rules even with nothing enumerated.
    assert "by design" in out


def test_running_tests_points_at_harness_not_app():
    out = _render()
    assert f"{PROBE_HARNESS_DIR}/tests/" in out
    # Still names multiple possible verifier scripts rather than one hard path.
    assert "run_tests.sh" in out
    assert "test.sh" in out
    # Explicitly redirects stale "/app/tests" directive references.
    assert "stale" in out


def test_render_cli_section_present():
    out = _render()
    assert QUERY_CLI_CONTAINER_PATH in out
    assert "node /probe-harness/oddish-query" in out
    assert "RELATED TRIAL LOGS" not in out
