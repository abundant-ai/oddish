import asyncio
from pathlib import Path

from oddish.worker.probe_staging import (
    apply_probe_overlay,
    read_query_cli_text,
    stage_query_cli,
    stage_cli_mount,
)
from oddish.worker.probe_overlay import render_probe_instruction, PROBE_SYSTEM_FRAMING


def test_stage_query_cli_copies_executable(tmp_path):
    stage_query_cli(tmp_path)
    cli = tmp_path / "oddish-query"
    assert cli.exists() and cli.read_text().startswith("#!/usr/bin/env node")


def test_instruction_documents_cli_not_related_logs():
    md = render_probe_instruction(
        PROBE_SYSTEM_FRAMING, "do x", "orig", time_budget_sec=600, probe_only_paths=[]
    )
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


def test_apply_overlay_stages_no_probe_only_dirs(tmp_path: Path):
    task = tmp_path / "task"
    task.mkdir()
    (task / "instruction.md").write_text("ORIGINAL")
    asyncio.run(
        apply_probe_overlay(
            task, task_id="t1", trial_id="tr1", extra_instructions="DO THE PROBE"
        )
    )
    # No probe-only material staged into the task dir.
    assert not (task / "harbor_src").exists()
    assert not (task / "000-READ-ME-PROBE-ONLY.txt").exists()
    # Brief saved + instruction rewritten.
    assert (task / "AGENT_BRIEF.md").read_text() == "ORIGINAL"
    assert "DO THE PROBE" in (task / "instruction.md").read_text()


def test_read_query_cli_text_returns_node_shebang():
    text = read_query_cli_text()
    assert text.startswith("#!/usr/bin/env node")
