from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from oddish.core import trial_io


def _step_doc(session: str, start_minute: int, n_steps: int) -> dict:
    return {
        "schema_version": "ATIF-v1",
        "session_id": session,
        "agent": {"name": "claude-code"},
        "steps": [
            {
                "step_id": i + 1,
                "timestamp": f"2026-09-03T05:{start_minute + i:02d}:00.000Z",
                "source": "agent",
                "message": f"{session}-step-{i + 1}",
            }
            for i in range(n_steps)
        ],
        "final_metrics": {"total_steps": n_steps},
    }


def test_merge_orders_by_time_and_renumbers_step_ids():
    fix = _step_doc("fix-session", 10, 3)
    triage = _step_doc("triage-session", 1, 2)
    # Alphabetical key order would put "fix" first; timestamps say triage ran first.
    merged = trial_io._merge_step_trajectories([("fix", fix), ("triage", triage)])

    assert merged is not None
    assert merged["session_id"] == "triage-session"
    assert merged["harbor_steps"] == ["triage", "fix"]
    assert [s["step_id"] for s in merged["steps"]] == [1, 2, 3, 4, 5]
    assert [s["harbor_step"] for s in merged["steps"]] == [
        "triage",
        "triage",
        "fix",
        "fix",
        "fix",
    ]
    assert merged["final_metrics"] == {"total_steps": 3}


def test_merge_single_step_returns_doc_unchanged():
    doc = _step_doc("solo", 1, 2)
    merged = trial_io._merge_step_trajectories([("solve", doc)])
    assert merged is doc


def test_exact_layout_reads_multi_step_trajectories(monkeypatch):
    prefix = "tasks/task-1/trials/trial-steps/"
    selected = f"{prefix}run-1/"
    objects = {
        f"{prefix}result.json": json.dumps(
            {"trial_results": [{"trial_name": "run-1"}]}
        ),
        f"{selected}result.json": json.dumps(
            {
                "step_results": [
                    {"step_name": "triage"},
                    {"step_name": "fix"},
                ]
            }
        ),
        f"{selected}steps/triage/agent/trajectory.json": json.dumps(
            _step_doc("triage-session", 1, 2)
        ),
        f"{selected}steps/fix/agent/trajectory.json": json.dumps(
            _step_doc("fix-session", 10, 3)
        ),
    }

    class StepStorage:
        async def object_exists(self, key: str) -> bool:
            return key in objects

        async def download_text(self, key: str) -> str:
            return objects[key]

        async def list_keys(self, prefix: str) -> list[str]:
            raise AssertionError("exact reads must not scan directories")

    monkeypatch.setattr(trial_io, "get_storage_client", lambda: StepStorage())
    trial = SimpleNamespace(
        id="trial-steps",
        name="trial-steps",
        model="global.anthropic.claude-opus-4-8",
        trial_s3_key=prefix,
        attempts=1,
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc),
    )

    trajectory = asyncio.run(trial_io.read_trial_trajectory(trial))

    assert trajectory is not None
    assert trajectory["harbor_steps"] == ["triage", "fix"]
    assert len(trajectory["steps"]) == 5
    assert trajectory["steps"][0]["message"] == "triage-session-step-1"
    assert trajectory["steps"][-1]["message"] == "fix-session-step-3"
