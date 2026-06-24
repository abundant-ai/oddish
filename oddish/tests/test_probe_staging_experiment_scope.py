import pytest

from oddish.worker import probe_staging
from oddish.worker.probe_overlay import QUERY_CLI_CONTAINER_PATH


@pytest.mark.asyncio
async def test_apply_overlay_stages_query_cli(tmp_path, monkeypatch):
    """apply_probe_overlay must stage the oddish-query CLI in the work dir."""
    # Minimal instruction.md so apply_probe_overlay can write the overlay.
    (tmp_path / "instruction.md").write_text("original task")

    # Stub out harbor source staging (not under test here).
    monkeypatch.setattr(probe_staging, "stage_harbor_source", lambda d: True)

    await probe_staging.apply_probe_overlay(
        tmp_path,
        task_id="task-A",
        trial_id="trial-1",
        extra_instructions="do x",
        probe_scope="experiment",
    )

    cli = tmp_path / "oddish-query"
    assert cli.exists(), "oddish-query must be staged in the work dir"
    assert cli.read_text().startswith("#!/usr/bin/env node")


@pytest.mark.asyncio
async def test_apply_overlay_instruction_documents_cli(tmp_path, monkeypatch):
    """The rendered instruction.md must document the CLI and not mention related trial logs."""
    (tmp_path / "instruction.md").write_text("original task")
    monkeypatch.setattr(probe_staging, "stage_harbor_source", lambda d: True)

    await probe_staging.apply_probe_overlay(
        tmp_path,
        task_id="task-A",
        trial_id="trial-1",
        extra_instructions="do x",
    )

    instr = (tmp_path / "instruction.md").read_text()
    assert "oddish-query" in instr
    assert f"node {QUERY_CLI_CONTAINER_PATH}" in instr
    assert "RELATED TRIAL LOGS" not in instr
    # related_trials/ directory must NOT be staged
    assert not (tmp_path / "related_trials").exists()
