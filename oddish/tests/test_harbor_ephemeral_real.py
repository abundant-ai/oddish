"""Real older-Harbor end-to-end check for the ephemeral engine.

Spawns the ACTUAL child via ``uv run --no-project --with "harbor @ git+...@<sha>"
--python 3.13`` against a real older Harbor commit on the locked fork, proving
the override env resolves+builds, the standalone child imports that Harbor and
runs Harbor's ``Job`` API, and the parent bridges its outcome back. The task is
intentionally invalid so the run fails fast (no Docker build / API keys needed);
what's validated is the uv-child-bridge round trip, not a green trial.

Needs network + uv (first run resolves the env, ~15s; cached after). Skipped
automatically if uv is unavailable.
"""

from __future__ import annotations

import shutil

import pytest

from oddish.config import HARBOR_DEFAULT_SOURCE
from oddish.workers.harbor.ephemeral import run_ephemeral_harbor_trial

pytestmark = pytest.mark.asyncio

# An older commit on the locked fork (predates the default pin; still v0.13.1, so
# the JobConfig API matches what the child rebuilds).
_OLDER_HARBOR_SHA = "07a576944f95c3f57147ba35a7f3ec97910d6141"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not available")
async def test_real_older_harbor_child_runs_and_bridges_outcome(tmp_path):
    # A task dir with no task.toml -> the child's real older Harbor fails fast
    # while building the Job, exercising import + JobConfig + outcome bridge.
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    harbor_config = {
        "mode": "probe",  # skip parent-side task-timeout validation
        "variant_id": "ephemeral",
        "source": HARBOR_DEFAULT_SOURCE,
        "resolved_sha": _OLDER_HARBOR_SHA,
    }

    outcome = await run_ephemeral_harbor_trial(
        task_path=task_dir,
        agent="nop",
        jobs_dir=tmp_path / "jobs",
        model=None,
        trial_id="t-real-eph",
        harbor_config=harbor_config,
    )

    # The child started (uv resolved the older Harbor and imported it) and the
    # parent bridged a terminal outcome -- no result.json from an invalid task.
    assert outcome is not None
    assert outcome.exception_type == "HarborOverrideImportError"
    assert outcome.error  # a non-empty failure summary from the child
    assert outcome.job_dir is not None
