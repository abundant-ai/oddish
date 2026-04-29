from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.services.cc_chat.file_store import LocalFileStore
from api.services.cc_chat.orchestrator import CCChatOrchestrator

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_BASE = REPO_ROOT / "jobs"
FIXTURE_EXPERIMENT_ID = "2026-04-26__16-45-36"


def _make_orchestrator(fake_daytona) -> CCChatOrchestrator:
    return CCChatOrchestrator(
        daytona=fake_daytona,
        file_store=LocalFileStore(base_path=FIXTURE_BASE),
        anthropic_api_key="sk-test",
        auto_stop_minutes=30,
    )


@pytest.mark.asyncio
async def test_start_creates_sandbox_and_uploads_files(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    sid = await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")
    assert sid

    # Exactly one sandbox created with the expected env
    assert len(fake_daytona.created) == 1
    sbx = fake_daytona.created[0]
    assert sbx.env_vars["ANTHROPIC_API_KEY"] == "sk-test"
    assert sbx.auto_stop_minutes == 30

    # CLAUDE.md was written
    assert "/workspace/CLAUDE.md" in sbx.files
    claude_md = sbx.files["/workspace/CLAUDE.md"].decode()
    assert FIXTURE_EXPERIMENT_ID in claude_md
    # At least one trial id appears
    assert "hello-world__eU7yQqg" in claude_md

    # Experiment files are under /workspace/jobs/<experiment_id>/
    expected_path = (
        f"/workspace/jobs/{FIXTURE_EXPERIMENT_ID}/"
        "hello-world__eU7yQqg/result.json"
    )
    assert expected_path in sbx.files
    # And the contents match disk
    on_disk = (
        FIXTURE_BASE
        / FIXTURE_EXPERIMENT_ID
        / "hello-world__eU7yQqg"
        / "result.json"
    ).read_bytes()
    assert sbx.files[expected_path] == on_disk

    # A daytona session named "cc" was created
    assert "cc" in sbx.sessions

    # Registry entry exists
    state = orch._sessions.get(sid)
    assert state is not None
    assert state.experiment_id == FIXTURE_EXPERIMENT_ID
    assert state.org_id == "org-1"
    assert state.claude_session_id is None


@pytest.mark.asyncio
async def test_start_installs_claude_cli(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")
    install_execs = [
        e for e in fake_daytona.execs
        if any("@anthropic-ai/claude-code" in arg for arg in e["command"])
    ]
    assert install_execs, (
        "expected an exec call that installs the claude CLI"
    )


@pytest.mark.asyncio
async def test_start_aborts_and_deletes_on_upload_failure(
    fake_daytona, monkeypatch
):
    orch = _make_orchestrator(fake_daytona)

    real_upload = fake_daytona.upload_file

    async def flaky_upload(sandbox, **kwargs):
        if "trial.log" in kwargs["dest_path"]:
            raise RuntimeError("boom")
        await real_upload(sandbox, **kwargs)

    monkeypatch.setattr(fake_daytona, "upload_file", flaky_upload)

    with pytest.raises(RuntimeError, match="boom"):
        await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")

    # Sandbox was created and then deleted
    assert len(fake_daytona.created) == 1
    assert fake_daytona.created[0].deleted is True
    # No session lingers in the registry
    assert list(orch._sessions._sessions.keys()) == []
