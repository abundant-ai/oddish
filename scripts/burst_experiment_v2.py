"""Multi-task experiment: fan out across tasks AND wrappers.

For each of N tasks, submit a sweep with multiple agents. Verifies the
wrapper architecture handles real diversity:
  - process_single_job_baseline  (nop, oracle)
  - process_single_job_gemini    (terminus-2 / gemini)
  - process_single_job_claude    (claude-code / claude, optional)
  - process_single_job_openai    (codex / gpt, optional)

Notes on fairness:
  All preview tasks share a single owner, so the per-user fairness
  join in the claim SQL fires with the same key for every row.  To
  *test* user-level fairness you'd need tasks owned by distinct
  users, which this preview can't easily produce.  What this test
  shows is per-WRAPPER concurrency (each wrapper drains independently
  under its own max_containers) and whether one wrapper saturating
  blocks another.

Usage:
  ODDISH_API_KEY=... ODDISH_API_URL=... uv run --with httpx \
    python scripts/burst_experiment_v2.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import defaultdict

import httpx


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        print(f"missing env var {name}", file=sys.stderr); sys.exit(2)
    return v


async def list_tasks(client: httpx.AsyncClient, *, limit: int) -> list[dict]:
    r = await client.get(f"/tasks?limit={limit}", timeout=30.0)
    r.raise_for_status()
    return r.json()


async def submit_sweep(
    client: httpx.AsyncClient,
    *,
    task_id: str,
    configs: list[dict],
) -> list[str]:
    payload = {
        "task_id": task_id,
        "append_to_task": True,
        "configs": configs,
        "priority": "low",
    }
    r = await client.post("/tasks/sweep", json=payload, timeout=300.0)
    r.raise_for_status()
    return r.json().get("new_trial_ids", [])


async def queue_state(client: httpx.AsyncClient) -> dict:
    r = await client.get("/admin/queue-status", timeout=30.0)
    r.raise_for_status()
    return r.json()


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--n-tasks", type=int, default=5)
    p.add_argument("--trials-per-agent", type=int, default=3)
    p.add_argument("--watch-seconds", type=float, default=420.0)
    p.add_argument("--poll-interval", type=float, default=15.0)
    args = p.parse_args()

    api_url = _env("ODDISH_API_URL").rstrip("/")
    headers = {"Authorization": f"Bearer {_env('ODDISH_API_KEY')}"}

    configs = [
        {"agent": "nop", "n_trials": args.trials_per_agent},
        {"agent": "oracle", "n_trials": args.trials_per_agent},
        {"agent": "terminus-2", "model": "gemini/gemini-3.1-pro-preview",
         "n_trials": args.trials_per_agent},
    ]

    async with httpx.AsyncClient(base_url=api_url, headers=headers) as client:
        all_tasks = await list_tasks(client, limit=args.n_tasks + 5)
        # Skip the heavily-loaded signalk task so we exercise multiple
        # task IDs evenly.
        chosen = [
            t for t in all_tasks
            if not t["id"].startswith("signalk-")
        ][: args.n_tasks]
        if len(chosen) < args.n_tasks:
            chosen = all_tasks[: args.n_tasks]

        print(f"chosen {len(chosen)} tasks:")
        for t in chosen:
            print(f"  {t['id']}")
        print()

        # --- Submit one sweep per task, in parallel ---
        t0 = time.monotonic()
        print(f"[{time.monotonic()-t0:>6.1f}s] firing {len(chosen)} sweeps in parallel...")
        results = await asyncio.gather(
            *(
                submit_sweep(client, task_id=t["id"], configs=configs)
                for t in chosen
            ),
            return_exceptions=True,
        )

        mine: dict[str, str] = {}  # trial_id -> task_id
        for task, res in zip(chosen, results):
            if isinstance(res, BaseException):
                print(f"  submit failed for {task['id']}: {res!r}")
                continue
            for tid in res:
                mine[tid] = task["id"]
            print(f"  {task['id']}: {len(res)} trials")
        print(f"[{time.monotonic()-t0:>6.1f}s] all sweeps submitted; total trials = {len(mine)}")
        print()

        # --- Watch loop: per-task and per-wrapper rollup ---
        deadline = time.monotonic() + args.watch_seconds
        while time.monotonic() < deadline:
            try:
                q = await queue_state(client)
            except Exception as exc:
                print(f"  queue fetch error: {exc!r}"); await asyncio.sleep(args.poll_interval); continue

            elapsed = time.monotonic() - t0
            queues = q.get("queues", [])
            tally = ", ".join(
                f"{qq['queue_key']}(q={qq.get('queued',0)} r={qq.get('running',0)})"
                for qq in queues
            )
            print(f"[{elapsed:>6.1f}s]  queues: {tally if tally else '(empty)'}")

            if not queues:
                # All work drained
                print(f"[{elapsed:>6.1f}s] all queues empty; exiting")
                break
            await asyncio.sleep(args.poll_interval)

        # Final per-task summary
        try:
            print("\n=== final per-task status of our trials ===")
            seen_tasks = set(mine.values())
            for tid_task in sorted(seen_tasks):
                r = await client.get(f"/tasks/{tid_task}/trials", timeout=30.0)
                if r.status_code != 200:
                    continue
                trials = {t["id"]: t for t in r.json()}
                ours = {tid: trials.get(tid) for tid in mine if mine[tid] == tid_task}
                counts: dict[tuple[str, str], int] = defaultdict(int)
                for tid, t in ours.items():
                    if t is None: continue
                    counts[(t.get("agent","?"), (t.get("status","?")).lower())] += 1
                summary = ", ".join(f"{a}={s}:{n}" for (a, s), n in sorted(counts.items()))
                print(f"  {tid_task}: {summary}")
        except Exception as exc:
            print(f"final summary failed: {exc!r}")


if __name__ == "__main__":
    asyncio.run(main())
