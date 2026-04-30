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
    assert "/home/daytona/workspace/CLAUDE.md" in sbx.files
    claude_md = sbx.files["/home/daytona/workspace/CLAUDE.md"].decode()
    assert FIXTURE_EXPERIMENT_ID in claude_md
    # At least one trial id appears
    assert "hello-world__eU7yQqg" in claude_md

    # Experiment files are under <_WORKSPACE_ROOT>/jobs/<experiment_id>/
    expected_path = (
        f"/home/daytona/workspace/jobs/{FIXTURE_EXPERIMENT_ID}/"
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


@pytest.mark.asyncio
async def test_send_first_turn_captures_claude_session_id(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    sid = await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")

    fake_daytona.canned_stdout_chunks = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "cc-uuid-1"}) + "\n",
        json.dumps({"type": "assistant", "delta": "Hello"}) + "\n",
        json.dumps({"type": "result", "stop_reason": "end_turn"}) + "\n",
    ]

    events = [event async for event in orch.send(session_id=sid, content="hi")]
    types = [e["type"] for e in events]
    assert types == ["system", "assistant", "result"]
    assert orch._sessions.get(sid).claude_session_id == "cc-uuid-1"


@pytest.mark.asyncio
async def test_send_second_turn_passes_resume(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    sid = await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")

    # Turn 1
    fake_daytona.canned_stdout_chunks = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "cc-uuid-1"}) + "\n",
        json.dumps({"type": "result"}) + "\n",
    ]
    _ = [e async for e in orch.send(session_id=sid, content="first")]

    # Turn 2
    fake_daytona.execs.clear()
    fake_daytona.canned_stdout_chunks = [
        json.dumps({"type": "result"}) + "\n",
    ]
    _ = [e async for e in orch.send(session_id=sid, content="second")]

    claude_execs = [
        e for e in fake_daytona.execs
        if e["command"] and e["command"][0] == "claude"
    ]
    assert claude_execs, "expected a claude exec on the second turn"
    assert "--resume" in claude_execs[-1]["command"]
    assert "cc-uuid-1" in claude_execs[-1]["command"]


@pytest.mark.asyncio
async def test_send_unknown_session_raises(fake_daytona):
    from api.services.cc_chat.orchestrator import SessionNotFound
    orch = _make_orchestrator(fake_daytona)
    with pytest.raises(SessionNotFound):
        async for _ in orch.send(session_id="nope", content="hi"):
            pass


@pytest.mark.asyncio
async def test_close_deletes_sandbox_and_removes_state(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    sid = await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")
    assert orch._sessions.get(sid) is not None

    await orch.close(session_id=sid)

    assert orch._sessions.get(sid) is None
    assert fake_daytona.created[0].deleted is True


@pytest.mark.asyncio
async def test_close_unknown_session_is_idempotent(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    # No raise
    await orch.close(session_id="never-existed")


@pytest.mark.asyncio
async def test_start_injects_skills_when_skills_dir_set(fake_daytona, tmp_path):
    skill_root = tmp_path / "skills"
    (skill_root / "eval-task-analysis").mkdir(parents=True)
    (skill_root / "eval-task-analysis" / "SKILL.md").write_text(
        "---\nname: eval-task-analysis\ndescription: test\n---\nbody"
    )
    (skill_root / "eval-task-analysis" / "references").mkdir()
    (skill_root / "eval-task-analysis" / "references" / "notes.md").write_text("hi")

    orch = CCChatOrchestrator(
        daytona=fake_daytona,
        file_store=LocalFileStore(base_path=FIXTURE_BASE),
        anthropic_api_key="sk-test",
        auto_stop_minutes=30,
        skills_dir=skill_root,
    )
    await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")
    sbx = fake_daytona.created[0]

    skill_md = "/home/daytona/workspace/.claude/skills/eval-task-analysis/SKILL.md"
    nested = (
        "/home/daytona/workspace/.claude/skills/"
        "eval-task-analysis/references/notes.md"
    )
    assert skill_md in sbx.files
    assert nested in sbx.files
    assert b"eval-task-analysis" in sbx.files[skill_md]


@pytest.mark.asyncio
async def test_start_skips_skills_when_no_dir(fake_daytona):
    orch = _make_orchestrator(fake_daytona)  # default skills_dir=None
    await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")
    sbx = fake_daytona.created[0]
    skill_paths = [p for p in sbx.files if "/.claude/skills/" in p]
    assert skill_paths == []
