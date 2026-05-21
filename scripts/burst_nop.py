"""Burst-load test for the dispatch reactor + DB proxy.

Submits N nop and oracle trials against an existing task and measures:
- Submit-to-RUNNING latency (per trial)
- Submit-to-SUCCESS latency (per trial)
- Wall-clock for the full burst

Usage:
    ODDISH_API_KEY=ok_pr-144_... \
    ODDISH_API_URL=https://abundant-ai-preview--oddish-pr-144-api.modal.run \
    python scripts/burst_nop.py --task-id <task_id> --n 100
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
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
    client: httpx.AsyncClient, *, task_id: str, n: int
) -> tuple[str, list[str]]:
    payload = {
        "task_id": task_id,
        "append_to_task": True,
        "configs": [
            {"agent": "nop", "n_trials": n},
            {"agent": "oracle", "n_trials": n},
        ],
        "priority": "low",
    }
    r = await client.post("/tasks/sweep", json=payload, timeout=60.0)
    r.raise_for_status()
    body = r.json()
    trial_ids = [t["id"] for t in body.get("trials", [])]
    return body.get("task_id", task_id), trial_ids


async def fetch_trial_status(
    client: httpx.AsyncClient, *, trial_id: str
) -> dict | None:
    r = await client.get(f"/trials/{trial_id}", timeout=10.0)
    if r.status_code != 200:
        return None
    return r.json()


async def watch_trial(
    client: httpx.AsyncClient,
    *,
    trial_id: str,
    submitted_at: float,
    poll_interval: float,
    deadline: float,
) -> dict:
    running_at: float | None = None
    terminal_at: float | None = None
    terminal_status: str | None = None
    while time.monotonic() < deadline:
        status = await fetch_trial_status(client, trial_id=trial_id)
        if status is not None:
            s = status.get("status")
            if running_at is None and s in {"RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"}:
                running_at = time.monotonic()
            if s in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                terminal_at = time.monotonic()
                terminal_status = s
                break
        await asyncio.sleep(poll_interval)
    return {
        "trial_id": trial_id,
        "submit_to_running_s": (running_at - submitted_at) if running_at else None,
        "submit_to_terminal_s": (terminal_at - submitted_at) if terminal_at else None,
        "terminal_status": terminal_status,
    }


def summarize(samples: Iterable[float | None], label: str) -> None:
    values = sorted(v for v in samples if v is not None)
    if not values:
        print(f"  {label}: no data")
        return
    p50 = values[len(values) // 2]
    p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
    p99 = values[min(len(values) - 1, int(len(values) * 0.99))]
    print(
        f"  {label}: "
        f"n={len(values)} "
        f"min={min(values):.2f}s "
        f"p50={p50:.2f}s "
        f"p95={p95:.2f}s "
        f"p99={p99:.2f}s "
        f"max={max(values):.2f}s "
        f"mean={statistics.mean(values):.2f}s"
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True, help="Existing task to sweep")
    parser.add_argument(
        "--n", type=int, default=100, help="Trials per agent (default 100)"
    )
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument(
        "--deadline-seconds", type=float, default=600.0, help="Per-trial watch deadline"
    )
    args = parser.parse_args()

    api_url = _env("ODDISH_API_URL").rstrip("/")
    api_key = _env("ODDISH_API_KEY")

    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(base_url=api_url, headers=headers) as client:
        print(f"submitting {args.n}x nop + {args.n}x oracle against {args.task_id}...")
        t0 = time.monotonic()
        _, trial_ids = await submit_sweep(client, task_id=args.task_id, n=args.n)
        submit_elapsed = time.monotonic() - t0
        print(f"submitted {len(trial_ids)} trials in {submit_elapsed:.2f}s")

        deadline = time.monotonic() + args.deadline_seconds
        watchers = [
            watch_trial(
                client,
                trial_id=tid,
                submitted_at=t0,
                poll_interval=args.poll_interval,
                deadline=deadline,
            )
            for tid in trial_ids
        ]
        results = await asyncio.gather(*watchers)

    total_elapsed = time.monotonic() - t0
    print(f"\nwall-clock to last terminal: {total_elapsed:.2f}s")
    print(f"submit call: {submit_elapsed:.2f}s")
    print("\nlatencies (from sweep POST):")
    summarize((r["submit_to_running_s"] for r in results), "submit -> RUNNING")
    summarize((r["submit_to_terminal_s"] for r in results), "submit -> terminal")

    status_counts: dict[str, int] = {}
    for r in results:
        s = r["terminal_status"] or "TIMEOUT"
        status_counts[s] = status_counts.get(s, 0) + 1
    print("\nterminal status counts:", status_counts)


if __name__ == "__main__":
    asyncio.run(main())
