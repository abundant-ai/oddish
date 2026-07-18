"""Read-only probe: is the legacy migration implicated in the QA job backlog?

ONE-OFF INCIDENT DIAGNOSTIC -- TEMPORARY BY DESIGN. Every statement is a SELECT.

The question this answers: a dev reports Task QA jobs not progressing (48 running,
350 trials running, 166 queued, 114 retrying). Two candidate causes:

  (A) The migration enqueued QA work. Validator Layer 5 reported 0, but it only
      counts jobs whose SUBJECT is an imported trial/task. Legacy trials MERGE
      into pre-existing NATIVE tasks (74 of 89 task-groups in reflection-ai,
      8 of 8 in the pilot), and a QA job on a native task carries a NATIVE
      subject_id -- invisible to that check. This probe closes that gap by
      asking the reverse question: which QA jobs belong to tasks that now
      contain imported trials?

  (B) The transfers starved the live queue (pooler connections / lock
      contention). Discriminated by TIMELINE: did the backlog start inside the
      transfer windows, or before them?

Transfer windows (UTC, 2026-07-18): pilot ~1 min, pilot re-run ~1 min,
reflection-ai ~9 min at concurrency 16.

Usage:
    modal run backend/scripts/legacy_qa_incident_probe.py
"""

import os

import modal

app = modal.App("oddish-legacy-qa-probe")
image = modal.Image.debian_slim(python_version="3.13").pip_install("asyncpg")
secret = modal.Secret.from_name("oddish-prod", environment_name="main")

IMPORT_TAG = "sauron-migration"


@app.function(image=image, secrets=[secret], timeout=600)
async def probe() -> bool:
    import asyncpg

    url = os.environ["ODDISH_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url, statement_cache_size=0)

    async def show(title: str, sql: str, *args) -> list:
        print(f"\n== {title} ==")
        try:
            rows = await conn.fetch(sql, *args)
            if not rows:
                print("  (no rows)")
            for r in rows:
                print("  " + "  ".join(f"{k}={v}" for k, v in dict(r).items()))
            return rows
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] {exc!r}")
            return []

    # Introspect first so we never guess a column name.
    await show(
        "worker_jobs columns",
        """
        SELECT string_agg(column_name, ', ' ORDER BY ordinal_position) AS cols
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name='worker_jobs'
        """,
    )

    await show(
        "worker_jobs by kind x status",
        """
        SELECT kind, status, count(*) AS n,
               min(created_at) AS oldest, max(created_at) AS newest
        FROM worker_jobs
        GROUP BY kind, status
        ORDER BY n DESC
        LIMIT 40
        """,
    )

    # (B) TIMELINE. Did the non-terminal backlog predate today's transfers?
    await show(
        "non-terminal jobs by hour created (last 48h)",
        """
        SELECT date_trunc('hour', created_at) AS hour, kind, count(*) AS n
        FROM worker_jobs
        WHERE status NOT IN ('SUCCESS','FAILED','SKIPPED')
          AND created_at > now() - interval '48 hours'
        GROUP BY 1, 2
        ORDER BY 1 DESC, 3 DESC
        LIMIT 60
        """,
    )

    await show(
        "oldest non-terminal jobs (are they actually stuck?)",
        """
        SELECT kind, status, count(*) AS n,
               min(created_at) AS oldest_created,
               now() - min(created_at) AS age
        FROM worker_jobs
        WHERE status NOT IN ('SUCCESS','FAILED','SKIPPED')
        GROUP BY kind, status
        ORDER BY min(created_at) ASC
        LIMIT 20
        """,
    )

    # (A) THE VALIDATOR GAP. QA jobs on tasks that contain imported trials --
    # including NATIVE tasks the migration merged into.
    await show(
        "QA/analysis jobs on tasks CONTAINING imported trials (the gap)",
        """
        SELECT wj.kind, wj.status, count(*) AS n,
               min(wj.created_at) AS oldest, max(wj.created_at) AS newest
        FROM worker_jobs wj
        WHERE wj.subject_table = 'tasks'
          AND wj.subject_id IN (
                SELECT DISTINCT t.task_id FROM trials t
                WHERE t.imported_at IS NOT NULL
          )
        GROUP BY wj.kind, wj.status
        ORDER BY n DESC
        LIMIT 30
        """,
    )

    await show(
        "...of those, how many were created AFTER the first import today?",
        """
        SELECT wj.kind, wj.status, count(*) AS n
        FROM worker_jobs wj
        WHERE wj.subject_table = 'tasks'
          AND wj.subject_id IN (
                SELECT DISTINCT t.task_id FROM trials t
                WHERE t.imported_at IS NOT NULL
          )
          AND wj.created_at >= (SELECT min(imported_at) FROM trials WHERE imported_at IS NOT NULL)
        GROUP BY wj.kind, wj.status
        ORDER BY n DESC
        LIMIT 30
        """,
    )

    await show(
        "when did the migration actually write? (import windows)",
        """
        SELECT date_trunc('minute', imported_at) AS minute, count(*) AS trials
        FROM trials WHERE imported_at IS NOT NULL
        GROUP BY 1 ORDER BY 1
        LIMIT 40
        """,
    )

    # (B) Are we holding connections or locks right now?
    await show(
        "current DB activity by state",
        """
        SELECT state, count(*) AS n, max(now() - query_start) AS longest
        FROM pg_stat_activity
        WHERE datname = current_database()
        GROUP BY state ORDER BY n DESC
        """,
    )

    await show(
        "current blocking / waiting sessions",
        """
        SELECT pid, wait_event_type, wait_event, state,
               left(query, 90) AS query, now() - query_start AS elapsed
        FROM pg_stat_activity
        WHERE datname = current_database() AND wait_event_type = 'Lock'
        ORDER BY query_start
        LIMIT 20
        """,
    )

    # Do imported trials look terminal, as designed?
    await show(
        "imported trial status distribution (should be terminal, never RUNNING)",
        """
        SELECT status, count(*) AS n
        FROM trials WHERE imported_at IS NOT NULL
        GROUP BY status ORDER BY n DESC
        """,
    )

    await conn.close()
    return True


@app.local_entrypoint()
def main() -> None:
    probe.remote()
