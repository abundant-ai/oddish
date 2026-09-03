"""Multi-step (harbor ``[[steps]]``) trials store one ATIF trajectory per step
under ``steps/<name>/agent/trajectory.json`` and have no root
``agent/trajectory.json``. The trajectory reader must find and merge them in
both EXACT and LEGACY layouts instead of reporting no trajectory (or, in the
LEGACY listing fallback, silently returning only the alphabetically first
step)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from oddish.core import trial_io
from oddish.core.trial_artifacts import TrialArtifactLayout, TrialArtifactMode

# Shapes trimmed from a real two-step trial (faiss-silent-defect-convo, ATIF-v1.7).
TRIAGE_TRAJECTORY = {
    "schema_version": "ATIF-v1.7",
    "session_id": "session-triage",
    "agent": {"name": "claude-code", "version": "2.1.258", "model_name": "m"},
    "steps": [
        {
            "step_id": 1,
            "timestamp": "2026-09-02T20:03:18.998Z",
            "source": "user",
            "message": "report",
        },
        {
            "step_id": 2,
            "timestamp": "2026-09-02T20:03:40.000Z",
            "source": "agent",
            "message": "reply",
        },
    ],
    "final_metrics": {
        "total_prompt_tokens": 100,
        "total_completion_tokens": 10,
        "total_steps": 2,
        "extra": {"total_cache_read_input_tokens": 50, "service_tiers": ["standard"]},
    },
}
FIX_TRAJECTORY = {
    "schema_version": "ATIF-v1.7",
    "session_id": "session-fix",
    "agent": {"name": "claude-code", "version": "2.1.258", "model_name": "m"},
    "steps": [
        {
            "step_id": 1,
            "timestamp": "2026-09-02T20:30:50.852Z",
            "source": "user",
            "message": "answers",
        },
        {
            "step_id": 2,
            "timestamp": "2026-09-02T20:31:00.000Z",
            "source": "agent",
            "message": "patch",
        },
        {
            "step_id": 3,
            "timestamp": "2026-09-02T20:31:10.000Z",
            "source": "agent",
            "message": "done",
        },
    ],
    "final_metrics": {
        "total_prompt_tokens": 200,
        "total_completion_tokens": 30,
        "total_steps": 3,
        "extra": {"total_cache_read_input_tokens": 70, "service_tiers": ["standard"]},
    },
}

PREFIX = "tasks/task-1/trials/trial-1/"
STEP_KEYS = {
    # 'fix' sorts before 'triage': ordering must come from timestamps, not keys.
    f"{PREFIX}steps/fix/agent/trajectory.json": FIX_TRAJECTORY,
    f"{PREFIX}steps/triage/agent/trajectory.json": TRIAGE_TRAJECTORY,
}


# Harbor writes the trial's own result.json next to steps/; EXACT mode reads
# step names from it instead of listing (exact reads must not scan prefixes).
RESULT_JSON = {
    "step_results": [{"step_name": "triage"}, {"step_name": "fix"}],
}


class _StepStorage:
    def __init__(self):
        self.objects = {k: json.dumps(v) for k, v in STEP_KEYS.items()}
        self.objects[f"{PREFIX}result.json"] = json.dumps(RESULT_JSON)

    async def download_text(self, key: str) -> str:
        if key in self.objects:
            return self.objects[key]
        raise FileNotFoundError(key)

    async def object_exists(self, key: str) -> bool:
        return key in self.objects

    async def list_keys(self, prefix: str) -> list[str]:
        raise AssertionError("step reads must not scan prefixes")


def _trial() -> SimpleNamespace:
    return SimpleNamespace(
        id="trial-1",
        name="trial-1",
        model="m",
        trial_s3_key=PREFIX,
        harbor_result_path=None,
        finished_at=None,
    )


def _assert_merged(trajectory: dict | None) -> None:
    assert trajectory is not None
    steps = trajectory["steps"]
    assert [s["step_id"] for s in steps] == [1, 2, 3, 4, 5]
    # triage (earlier timestamps) first, despite 'fix' sorting first by key
    assert [s["extra"]["harbor_step"] for s in steps] == [
        "triage",
        "triage",
        "fix",
        "fix",
        "fix",
    ]
    metrics = trajectory["final_metrics"]
    assert metrics["total_prompt_tokens"] == 300
    assert metrics["total_steps"] == 5
    assert metrics["extra"]["total_cache_read_input_tokens"] == 120


def test_exact_layout_merges_step_trajectories():
    layout = TrialArtifactLayout(
        mode=TrialArtifactMode.EXACT,
        attempt_prefix=PREFIX,
        artifact_prefix=PREFIX,
    )
    trajectory = asyncio.run(
        trial_io._read_trial_trajectory_from_s3(_trial(), _StepStorage(), layout)
    )
    _assert_merged(trajectory)


def test_legacy_listing_merges_all_steps_not_just_first():
    layout = TrialArtifactLayout(
        mode=TrialArtifactMode.LEGACY,
        attempt_prefix=PREFIX,
        artifact_prefix=None,
        listed_keys=tuple(sorted(STEP_KEYS)),
    )
    trajectory = asyncio.run(
        trial_io._read_trial_trajectory_from_s3(_trial(), _StepStorage(), layout)
    )
    _assert_merged(trajectory)


def test_single_step_layout_unchanged():
    single = {
        "schema_version": "ATIF-v1.7",
        "session_id": "s",
        "agent": {"name": "a"},
        "steps": [{"step_id": 1, "timestamp": "2026-01-01T00:00:00Z"}],
    }

    class _RootStorage(_StepStorage):
        async def download_text(self, key: str) -> str:
            if key == f"{PREFIX}agent/trajectory.json":
                return json.dumps(single)
            raise FileNotFoundError(key)

        async def object_exists(self, key: str) -> bool:
            return key == f"{PREFIX}agent/trajectory.json"

        async def list_keys(self, prefix: str) -> list[str]:
            return []

    layout = TrialArtifactLayout(
        mode=TrialArtifactMode.EXACT,
        attempt_prefix=PREFIX,
        artifact_prefix=PREFIX,
    )
    trajectory = asyncio.run(
        trial_io._read_trial_trajectory_from_s3(_trial(), _RootStorage(), layout)
    )
    assert trajectory == single


def test_metrics_merge_tolerates_non_numeric_values():
    # One step reports null/str where another reports numbers (Bugbot finding):
    # the merge must sum what it can rather than raise and drop the trajectory.
    merged = trial_io._sum_numeric_metrics(
        [
            {"total_cost_usd": None, "runtime": "fast", "extra": {"cache": None}},
            {"total_cost_usd": 5.0, "runtime": 2, "extra": {"cache": 7}},
        ]
    )
    assert merged["total_cost_usd"] == 5.0
    assert merged["runtime"] == 2
    assert merged["extra"]["cache"] == 7


def test_exact_step_read_propagates_storage_failures():
    # A transient S3 failure must not read as "no trajectory" (which finished
    # trials would cache); it propagates like the root EXACT read (Bugbot).
    import pytest
    from botocore.exceptions import ClientError

    class _FlakyStorage(_StepStorage):
        async def download_text(self, key: str) -> str:
            if key.endswith("/agent/trajectory.json"):
                raise ClientError({"Error": {"Code": "500"}}, "GetObject")
            return await super().download_text(key)

    layout = TrialArtifactLayout(
        mode=TrialArtifactMode.EXACT,
        attempt_prefix=PREFIX,
        artifact_prefix=PREFIX,
    )
    with pytest.raises(ClientError):
        asyncio.run(
            trial_io._read_trial_trajectory_from_s3(_trial(), _FlakyStorage(), layout)
        )
