"""Control candidate admission in the explicitly selected Modal environment.

Run from backend: MODAL_ENVIRONMENT=staging MODAL_SECRET_ENVIRONMENT=main
MODAL_APP_NAME=oddish-staging uv run modal run --env staging worker_resource_rollout.py
Default invocation only displays the control. --fraction 0 stops new candidate
claims; --fraction .01 --max-workers 2 enables the deployed CPU-only candidate.
"""

import math
import modal

from modal_app import (
    WORKER_CANDIDATE_CONFIGURATION,
    WORKER_CANDIDATE_MAX_CONTAINERS,
    image,
    runtime_secrets,
)
from modal_runtime import MODAL_APP_NAME

app = modal.App(f"{MODAL_APP_NAME}-worker-resource-control")


@app.function(image=image, secrets=runtime_secrets, timeout=60, cpu=0.25, memory=512)
async def control(fraction: float | None = None, max_workers: int | None = None):
    from oddish.workers.queue.worker_job_single_job import _open_connection

    conn = await _open_connection()
    try:
        if fraction is not None:
            if not math.isfinite(fraction) or not 0 <= fraction <= 1:
                raise ValueError("fraction must be between 0 and 1")
            if (
                max_workers is not None
                and not 0 <= max_workers <= WORKER_CANDIDATE_MAX_CONTAINERS
            ):
                raise ValueError(
                    "max_workers must fit the deployed Modal container cap"
                )
            # This UPDATE conflicts with the claim's brief FOR SHARE lock. Once
            # it commits, all new claims observe the stop, including warm workers.
            await conn.execute(
                "UPDATE worker_resource_rollout SET fraction=$1, "
                "max_workers=COALESCE($2, max_workers), configuration=$3 WHERE id=1",
                fraction,
                max_workers,
                WORKER_CANDIDATE_CONFIGURATION,
            )
        elif max_workers is not None:
            raise ValueError("Set --fraction explicitly when changing max-workers")
        row = await conn.fetchrow("SELECT * FROM worker_resource_rollout WHERE id=1")
        if row is None:
            raise RuntimeError("Apply the worker_resources_001 core migration first")
        return dict(row)
    finally:
        await conn.close()


@app.local_entrypoint()
def main(fraction: float | None = None, max_workers: int | None = None):
    print(control.remote(fraction, max_workers))
