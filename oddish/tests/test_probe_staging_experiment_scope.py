import pytest

from oddish.worker import probe_staging
from oddish.worker.probe_overlay import QUERY_CLI_CONTAINER_PATH


@pytest.mark.asyncio
async def test_apply_overlay_does_not_stage_query_cli_in_work_dir(tmp_path):
    """apply_probe_overlay must NOT stage the oddish-query CLI in the work dir.

    The work dir is uploaded to the hidden stage; the CLI is delivered
    separately to /probe-harness via stage_cli_mount. Staging it here too would
    leave a redundant, never-referenced copy in the hidden stage.
    """
    # Minimal instruction.md so apply_probe_overlay can write the overlay.
    (tmp_path / "instruction.md").write_text("original task")

    await probe_staging.apply_probe_overlay(
        tmp_path,
        task_id="task-A",
        trial_id="trial-1",
        extra_instructions="do x",
        probe_scope="experiment",
    )

    assert not (tmp_path / "oddish-query").exists(), (
        "oddish-query must NOT be staged in the work dir (delivered via stage_cli_mount)"
    )


@pytest.mark.asyncio
async def test_apply_overlay_instruction_documents_cli(tmp_path):
    """The rendered instruction.md must document the CLI and not mention related trial logs."""
    (tmp_path / "instruction.md").write_text("original task")

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
