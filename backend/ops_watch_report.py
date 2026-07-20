"""One-off: watch a running report's real progress from outside.

    cd backend
    uv run modal run -e main ops_watch_report.py --analyzer 8e672fc5
    uv run modal run -e main ops_watch_report.py --analyzer 8e672fc5 --follow-seconds 2400

Why this exists: the analyzer's per-event progress lines are `logger.info` in
analyzer_cohort.py, and the Modal worker never calls logging.basicConfig -- so
the root logger sits at WARNING and they never reach stdout. `modal app logs`
shows only job start/end. What survives outside the process:

  * worker_jobs heartbeat (30s cadence) -- liveness only.
  * the cohort's Daytona sandbox (labelled analyzer=<id>), whose findings.jsonl
    grows as the map phase emits MAP FINDING: lines.
  * the Daytona session command logs -- the agent's own stdout, which the
    worker polls via get_session_command_logs and then drops on the floor.

Sandboxes are ephemeral and torn down when a cohort finishes, so content is
only visible mid-flight. Note the handler spends ~20min bucketing before any
sandbox is created; "no sandbox" early on is normal, not a stall.

Import only modal_app -- importing endpoints/worker registers the dispatcher.
"""

from __future__ import annotations

import modal

from modal_app import image, runtime_secrets

app = modal.App("watch-report-oneoff")

OUT_DIR = "/home/daytona/workspace/out"
SESSION_ID = "cc"  # analyzer_cohort.py passes daytona_session_id="cc"


async def _db_snapshot(analyzer_id: str) -> dict | None:
    from sqlalchemy import text

    from oddish.db import get_session

    async with get_session() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT a.status AS report_status,
                           a.error,
                           w.status AS job_status,
                           w.attempts,
                           w.max_attempts,
                           NOW() - w.heartbeat_at AS heartbeat_age,
                           NOW() - w.started_at AS running_for
                    FROM analyzers a
                    LEFT JOIN worker_jobs w
                      ON w.subject_table = 'analyzers' AND w.subject_id = a.id
                    WHERE a.id = :aid
                    ORDER BY w.created_at DESC
                    LIMIT 1
                    """
                ),
                {"aid": analyzer_id},
            )
        ).mappings().first()
    return dict(row) if row else None


async def _probe_sandbox(sbx) -> None:
    bucket = (sbx.labels or {}).get("bucket", "?")
    print(f"  -- sandbox {sbx.id} bucket={bucket} state={sbx.state}")
    try:
        r = await sbx.process.exec(
            f"wc -l {OUT_DIR}/findings.jsonl 2>/dev/null | awk '{{print $1}}'; "
            f"wc -c {OUT_DIR}/reduce.json 2>/dev/null | awk '{{print $1}}'; "
            f"ps auxww 2>/dev/null | grep -c '[c]laude'"
        )
        vals = (r.result or "").split()
        findings, reduce_bytes, claude_procs = (vals + ["?", "?", "?"])[:3]
        print(
            f"     findings.jsonl={findings} lines  reduce.json={reduce_bytes}B  "
            f"claude procs={claude_procs}"
        )
    except Exception as exc:
        print(f"     exec failed: {exc!r}")

    # The agent's own stdout: what the worker polls and then discards.
    try:
        sess = await sbx.process.get_session(SESSION_ID)
        cmds = getattr(sess, "commands", None) or []
        for cmd in cmds[-2:]:
            cid = getattr(cmd, "id", None)
            logs = await sbx.process.get_session_command_logs(SESSION_ID, cid)
            body = logs if isinstance(logs, str) else str(logs)
            print(f"     [session cmd {cid}] exit={getattr(cmd, 'exit_code', None)}")
            print(f"     last stdout: ...{body[-600:]}")
    except Exception as exc:
        print(f"     session logs unavailable: {exc!r}")


@app.function(image=image, secrets=runtime_secrets, timeout=3600)
async def watch(analyzer_id: str, follow_seconds: int = 0, interval: int = 60) -> None:
    import asyncio
    import os
    import time

    from oddish.config import Settings

    Settings.db_use_null_pool = True

    key = os.environ.get("DAYTONA_API_KEY", "")
    if not key:
        print("DAYTONA_API_KEY unset; sandbox probe unavailable.")

    from daytona import AsyncDaytona, DaytonaConfig, ListSandboxesQuery

    d = AsyncDaytona(DaytonaConfig(api_key=key)) if key else None
    deadline = time.monotonic() + max(follow_seconds, 0)
    try:
        while True:
            stamp = time.strftime("%H:%M:%S")
            row = await _db_snapshot(analyzer_id)
            if not row:
                print(f"[{stamp}] no analyzer {analyzer_id}")
                return
            print(
                f"[{stamp}] report={row['report_status']} job={row['job_status']} "
                f"attempt={row['attempts']}/{row['max_attempts']} "
                f"running_for={row['running_for']} hb_age={row['heartbeat_age']}"
            )
            if row["error"]:
                print(f"          error: {row['error']}")

            if d:
                listed = d.list(ListSandboxesQuery(labels={"analyzer": analyzer_id}))
                sandboxes = []
                if hasattr(listed, "__aiter__"):
                    async for sbx in listed:
                        sandboxes.append(sbx)
                else:
                    sandboxes = list(await listed)
                if not sandboxes:
                    print("          no cohort sandbox yet (bucketing phase)")
                for sbx in sandboxes:
                    await _probe_sandbox(sbx)

            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(interval)
    finally:
        if d:
            await d.close()


@app.local_entrypoint()
def main(analyzer: str, follow_seconds: int = 0, interval: int = 60) -> None:
    watch.remote(
        analyzer_id=analyzer, follow_seconds=follow_seconds, interval=interval
    )
