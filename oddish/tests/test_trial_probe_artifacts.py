from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import trial_io


def _make_trial(**kwargs):
    """A duck-typed stand-in — the reader only reads a few attributes."""
    defaults = {
        "id": "task-abc-0",
        "result": None,
        "trial_s3_key": None,
        "harbor_result_path": None,
        "finished_at": None,
        "error_message": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture(autouse=True)
def _clear_cache():
    trial_io._PROBE_ARTIFACTS_CACHE.clear()
    trial_io._PROBE_ARTIFACTS_LOCKS.clear()
    yield


@pytest.mark.anyio
async def test_inlined_artifacts_preferred(monkeypatch):
    artifacts = {
        "agent_messages": [{"kind": "text", "text": "hi"}],
        "verifier_stdout": "28 passed",
        "trajectory": None,
        "watchdog_log": None,
    }
    trial = _make_trial(result={"_artifacts": artifacts})

    async def _fail_resolve(*a, **k):  # pragma: no cover
        raise AssertionError("should not download from S3 when inlined")

    monkeypatch.setattr(trial_io, "resolve_trial_directory", _fail_resolve)
    out = await trial_io.read_trial_probe_artifacts(trial)
    assert out == artifacts


@pytest.mark.anyio
async def test_cloud_trial_extracts_from_downloaded_dir(monkeypatch, tmp_path):
    """Cloud trials have empty `result`; artifacts come from the S3 dir."""
    agent_dir = tmp_path / "task__x" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "claude-code.txt").write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "fixing it"}]},
            }
        )
        + "\n"
    )
    verifier_dir = tmp_path / "task__x" / "verifier"
    verifier_dir.mkdir(parents=True)
    (verifier_dir / "test-stdout.txt").write_text("28 passed in 1.16s")

    cleaned: list[Path] = []

    async def _fake_resolve(*, trial_id, trial_s3_key, trial_result_path):
        return tmp_path, tmp_path, trial_s3_key

    monkeypatch.setattr(trial_io, "resolve_trial_directory", _fake_resolve)
    monkeypatch.setattr(trial_io, "_cleanup_temp_directory", cleaned.append)

    trial = _make_trial(
        result={},
        trial_s3_key="tasks/task-abc/trials/task-abc-0/",
        finished_at=None,
    )
    out = await trial_io.read_trial_probe_artifacts(trial)

    assert out["verifier_stdout"] == "28 passed in 1.16s"
    assert any("fixing it" in (m.get("text") or "") for m in out["agent_messages"])
    assert cleaned == [tmp_path]  # temp dir cleaned up


@pytest.mark.anyio
async def test_no_location_returns_empty(monkeypatch):
    trial = _make_trial(result={}, trial_s3_key=None, harbor_result_path=None)
    out = await trial_io.read_trial_probe_artifacts(trial)
    assert out["agent_messages"] == []
    assert out["verifier_stdout"] is None
