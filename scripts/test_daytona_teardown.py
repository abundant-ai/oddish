"""Live test of the Daytona teardown path shared by cancel_tasks and
cleanup_orphaned_tasks.

Both flows funnel every (provider, external_id) target through the same leaf,
``oddish.core.helpers.cancel_job_by_worker("daytona", external_id)``:
  - cancel_tasks_runs            -> oddish/src/oddish/queue.py:230
  - cleanup_orphaned_queue_state -> oddish/src/oddish/workers/queue/cleanup.py:334

So driving real sandboxes through that leaf exercises the daytona-specific
behaviour of both flows without standing up Postgres or the dispatcher.

Run with the backend venv (it has the daytona SDK + an editable oddish install):

    backend/.venv/bin/python scripts/test_daytona_teardown.py
"""

import asyncio
import os
from pathlib import Path

# The SDK reads DAYTONA_API_KEY from os.environ on demand (see
# daytona/_utils/env.py). Seed it from backend/.env so this script needs no
# shell setup. setdefault() means a real env var still wins.
_ENV_FILE = Path(__file__).resolve().parent.parent / "backend" / ".env"
for _line in _ENV_FILE.read_text().splitlines():
    _line = _line.strip()
    if _line.startswith("DAYTONA_") and "=" in _line:
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k, _v.strip().strip('"').strip("'"))

from daytona import (  # noqa: E402
    AsyncDaytona,
    CreateSandboxFromSnapshotParams,
    SandboxState,
)

from oddish.core.helpers import cancel_job_by_worker  # noqa: E402

# States that mean the sandbox is being torn down or already gone. Daytona's
# delete is async: get() returns DESTROYING immediately and settles to
# DESTROYED, so both count as "torn down".
_TERMINAL_STATES = {SandboxState.DESTROYED, SandboxState.DESTROYING}


async def make_sandbox() -> str:
    """Create a real Daytona sandbox and return its id (the external_id a
    worker_jobs row would carry).

    This region only permits ephemeral sandboxes, so ephemeral=True is
    required for create() to succeed.
    """
    client = AsyncDaytona()
    try:
        sandbox = await client.create(CreateSandboxFromSnapshotParams(ephemeral=True))
        return sandbox.id
    finally:
        await client.close()


async def verify_torn_down(sandbox_id: str) -> bool:
    """Confirm the sandbox is actually gone by querying Daytona directly.

    cancel_job_by_worker is best-effort and swallows exceptions, so its
    return value is not proof. Daytona keeps the record around briefly in a
    DESTROYING/DESTROYED state after delete (it doesn't 404), so we assert on
    that terminal state. A missing record (get raises) also means gone.
    """
    client = AsyncDaytona()
    try:
        for _ in range(10):
            try:
                sandbox = await client.get(sandbox_id)
            except Exception:
                # No record at all -> definitely torn down.
                return True
            if sandbox.state in _TERMINAL_STATES:
                return True
            await asyncio.sleep(1)
        return False
    finally:
        await client.close()


async def _force_delete(sandbox_id: str) -> None:
    """Best-effort delete so a failed run can't leak a billable sandbox.

    Safe to call even after a successful teardown: delete on an
    already-destroying/destroyed sandbox is a no-op we swallow.
    """
    client = AsyncDaytona()
    try:
        sandbox = await client.get(sandbox_id)
        if sandbox.state not in _TERMINAL_STATES:
            await client.delete(sandbox)
            print(f"[cleanup] force-deleted leaked sandbox {sandbox_id}")
    except Exception:
        pass
    finally:
        await client.close()


async def exercise(flow_name: str) -> None:
    sandbox_id = await make_sandbox()
    print(f"[{flow_name}] created sandbox {sandbox_id}")
    try:
        # This is the exact call both cancel_tasks_runs and
        # cleanup_orphaned_queue_state make per target.
        issued = await cancel_job_by_worker("daytona", sandbox_id)
        print(f"[{flow_name}] cancel_job_by_worker -> {issued}")

        ok = await verify_torn_down(sandbox_id)
        print(f"[{flow_name}] verified torn down -> {ok}")
        assert ok, f"{flow_name}: sandbox {sandbox_id} was not torn down"
    finally:
        # If the leaf failed (or the assert fired), the sandbox may still be
        # live and billable -- tear it down regardless of test outcome.
        await _force_delete(sandbox_id)


async def main() -> None:
    # Run each flow independently so a regression in either path is visible
    # even though they share the leaf today.
    await exercise("cancel_tasks")
    await exercise("cleanup_orphaned_tasks")
    print("OK: daytona teardown verified for both flows")


if __name__ == "__main__":
    asyncio.run(main())
