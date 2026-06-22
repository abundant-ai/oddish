from pathlib import Path

from oddish.worker.probe_staging import stage_query_cli
from oddish.worker.probe_overlay import render_probe_instruction, PROBE_SYSTEM_FRAMING


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
