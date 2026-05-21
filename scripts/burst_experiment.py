"""Multi-agent + fairness experiment for the wrapper architecture.

What it does:
  T=0   submit Sweep A from 'user-a': 30 nop, 30 oracle, 5 terminus-2.
  T=5s  submit Sweep B from 'user-b': 5 nop  (small competing batch).
  watch queues for N minutes; report progress per (user, agent).

What we check:
  1. nop / oracle (baseline wrapper)  run with no rate limit.
  2. terminus-2 (gemini wrapper)      runs concurrently, not blocked.
  3. user-b's 5 nop                   start running quickly even though
                                      user-a's 30 nop were submitted
                                      first -- the claim SQL's per-user
                                      fairness join steers workers
                                      toward the less-loaded user.

Usage:
  ODDISH_API_KEY=... ODDISH_API_URL=... uv run --with httpx \
    python scripts/burst_experiment.py --task-id <task>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Iterable

import httpx


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"missing env var {name}", file=sys.stderr)
        sys.exit(2)
    return value


async def submit_sweep(
    client: httpx.AsyncClient,
    *,
    task_id: str,
    user: str,
    configs: list[dict],
) -> list[str]:
    payload = {
        "task_id": task_id,
        "append_to_task": True,
        "configs": configs,
        "user": user,
        "priority": "low",
    }
    r = await client.post("/tasks/sweep", json=payload, timeout=300.0)
    r.raise_for_status()
    body = r.json()
    return body.get("new_trial_ids") or []


async def fetch_trials_map(client: httpx.AsyncClient, *, task_id: str) -> dict:
    r = await client.get(f"/tasks/{task_id}/trials", timeout=30.0)
    r.raise_for_status()
    return {t["id"]: t for t in r.json()}


def _summarize(trials: dict, mine: dict[str, str]) -> dict:
    """Group my submitted trial_ids by (user, agent, status)."""
    out: dict[tuple, dict[str, int]] = {}
    for tid, user_label in mine.items():
        row = trials.get(tid)
        if row is None:
            continue
        key = (user_label, row.get("agent"))
        st = (row.get("status") or "unknown").lower()
        out.setdefault(key, {}).setdefault(st, 0)
        out[key][st] += 1
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--watch-seconds", type=float, default=300.0)
    parser.add_argument("--poll-interval", type=float, default=10.0)
    args = parser.parse_args()

    api_url = _env("ODDISH_API_URL").rstrip("/")
    api_key = _env("ODDISH_API_KEY")
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(base_url=api_url, headers=headers) as client:
        # --- Sweep A from user-a: 30 nop + 30 oracle + 5 terminus-2 ---
        t0 = time.monotonic()
        print(f"[{time.monotonic()-t0:>6.1f}s] submitting Sweep A (user-a)...")
        a_ids = await submit_sweep(
            client,
            task_id=args.task_id,
            user="user-a",
            configs=[
                {"agent": "nop", "n_trials": 30},
                {"agent": "oracle", "n_trials": 30},
                {"agent": "terminus-2", "model": "gemini/gemini-3.1-pro-preview", "n_trials": 5},
            ],
        )
        print(f"[{time.monotonic()-t0:>6.1f}s]   Sweep A: submitted {len(a_ids)} trials")

        # --- Sweep B from user-b: just 5 nop, right after ---
        await asyncio.sleep(5)
        print(f"[{time.monotonic()-t0:>6.1f}s] submitting Sweep B (user-b)...")
        b_ids = await submit_sweep(
            client,
            task_id=args.task_id,
            user="user-b",
            configs=[{"agent": "nop", "n_trials": 5}],
        )
        print(f"[{time.monotonic()-t0:>6.1f}s]   Sweep B: submitted {len(b_ids)} trials")

        # Map every submitted id to the user that submitted it.
        mine = {**{tid: "user-a" for tid in a_ids}, **{tid: "user-b" for tid in b_ids}}

        # --- Watch loop ---
        deadline = time.monotonic() + args.watch_seconds
        while time.monotonic() < deadline:
            try:
                trials = await fetch_trials_map(client, task_id=args.task_id)
            except Exception as exc:
                print(f"[{time.monotonic()-t0:>6.1f}s] fetch error: {exc!r}")
                await asyncio.sleep(args.poll_interval)
                continue
            summary = _summarize(trials, mine)
            elapsed = time.monotonic() - t0
            line = f"[{elapsed:>6.1f}s] "
            for key in sorted(summary):
                statuses = summary[key]
                user, agent = key
                line += f"  {user}/{agent}={dict(sorted(statuses.items()))}"
            print(line)
            # Stop early if everything is terminal.
            non_terminal = sum(
                v
                for s in summary.values()
                for k, v in s.items()
                if k not in {"succeeded", "failed", "cancelled"}
            )
            if non_terminal == 0:
                print(f"[{elapsed:>6.1f}s] all trials terminal; exiting")
                break
            await asyncio.sleep(args.poll_interval)


if __name__ == "__main__":
    asyncio.run(main())
