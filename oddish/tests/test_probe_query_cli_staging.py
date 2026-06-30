from pathlib import Path

from oddish.worker.probe_staging import stage_query_cli, stage_cli_mount, write_boundary_markers
from oddish.worker.probe_overlay import render_probe_instruction, PROBE_SYSTEM_FRAMING, BOUNDARY_MARKER_NAME


def test_stage_query_cli_copies_executable(tmp_path):
    stage_query_cli(tmp_path)
    cli = tmp_path / "oddish-query"
    assert cli.exists() and cli.read_text().startswith("#!/usr/bin/env node")


def test_instruction_documents_cli_not_related_logs():
    md = render_probe_instruction(PROBE_SYSTEM_FRAMING, "do x", "orig",
                                  time_budget_sec=600, probe_only_paths=[])
    assert "oddish-query" in md
    assert "RELATED TRIAL LOGS" not in md
    assert "node /probe-harness/oddish-query" in md


def test_stage_cli_mount_writes_only_the_cli(tmp_path):
    harness = tmp_path / "harness"
    harness.mkdir()
    stage_cli_mount(harness)
    entries = sorted(p.name for p in harness.iterdir())
    assert entries == ["oddish-query"]
    cli = harness / "oddish-query"
    assert cli.read_text().startswith("#!/usr/bin/env node")
    # executable bit set
    assert cli.stat().st_mode & 0o111


def test_write_boundary_markers_plants_root_and_answer_key_dirs(tmp_path):
    (tmp_path / "solution").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "harbor_src").mkdir()
    write_boundary_markers(tmp_path)
    # Planted at root and in the answer-key subdirs...
    assert (tmp_path / BOUNDARY_MARKER_NAME).exists()
    assert (tmp_path / "solution" / BOUNDARY_MARKER_NAME).exists()
    assert (tmp_path / "tests" / BOUNDARY_MARKER_NAME).exists()
    # ...but NOT inside harbor_src (kept byte-for-byte exact).
    assert not (tmp_path / "harbor_src" / BOUNDARY_MARKER_NAME).exists()
    # The marker self-describes the boundary.
    body = (tmp_path / BOUNDARY_MARKER_NAME).read_text()
    assert "PROBE-ONLY" in body
    assert "oddish-query" in body
