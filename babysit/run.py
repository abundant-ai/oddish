"""
Babysitter for the two Oddish experiments.

Goal: ensure each (task, agent, model) pair within scope has at least
TARGET_VALID trials with status SUCCESS. Pairs in flight count toward the
target; failed/cancelled trials do not.

Skipped tasks: Embedding Eval, Kubernetes Rewrite, Next.js Rewrite, ZSTD Decoder.
Skipped pairs: nop / oracle baselines (always already covered, but we still
count them; they don't get re-kicked because deficit comes out zero).

When a pair has S < 5 and S+R < 5, we kick off (5 - S - R) new trials via
the oddish CLI in background mode (-b).
"""
from __future__ import annotations

import asyncio
import datetime
import os
import shlex
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# Make oddish package importable
sys.path.insert(0, "/workspace/oddish/src")

from sqlalchemy import text  # noqa: E402

from oddish.db import get_session  # noqa: E402

EXPERIMENTS = ["ad9a2ab3", "45a08302"]

# Tasks the user asked us to ignore in experiment ad9a2ab3.
SKIP_TASKS = {
    "embedding-eval-be1dc228",
    "kubernetes-rust-rewrite-bae0f616",
    "nextjs-vite-rewrite-b0d028b0",
    "zstd-decoder-72bbfded",
}

TARGET_VALID = 5

# Cap how many trials we kick per pair per iteration to avoid surprises.
PER_PAIR_MAX_PER_ITER = 3

# Loop sleep between checks.
SLEEP_SECONDS = 300

# Refresh (clean) the DB pool every iteration so we don't hold connections.
ODDISH_BIN = ["uv", "run", "oddish"]
ODDISH_DIR = "/workspace/oddish"

LOG_DIR = Path("/workspace/babysit/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "babysit.log", "a") as f:
        f.write(line + "\n")


async def fetch_pair_state() -> list[dict]:
    """
    Return one row per (experiment_id, task_id, agent, provider, model) with
    counts of trials by category.
    """
    sql = text(
        """
        SELECT experiment_id, task_id, agent, provider, model,
            COUNT(*) FILTER (WHERE status = 'SUCCESS') AS success_count,
            COUNT(*) FILTER (WHERE status IN ('RUNNING','QUEUED','PENDING','RETRYING')) AS active_count,
            COUNT(*) FILTER (WHERE status = 'FAILED') AS failed_count,
            COUNT(*) AS total
        FROM trials
        WHERE experiment_id = ANY(:exps)
          AND deleted_at IS NULL
        GROUP BY experiment_id, task_id, agent, provider, model
        ORDER BY experiment_id, task_id, agent, model
        """
    )
    out: list[dict] = []
    async with get_session() as s:
        r = await s.execute(sql, {"exps": EXPERIMENTS})
        for row in r:
            out.append(
                {
                    "experiment_id": row.experiment_id,
                    "task_id": row.task_id,
                    "agent": row.agent,
                    "provider": row.provider,
                    "model": row.model,
                    "success": row.success_count,
                    "active": row.active_count,
                    "failed": row.failed_count,
                    "total": row.total,
                }
            )
    return out


def kick_off_trials(experiment_id: str, task_id: str, agent: str, model: str, n: int) -> bool:
    """
    Kick off `n` new trials for the given pair via the oddish CLI in
    background mode. Returns True on apparent success.
    """
    # The oddish CLI accepts model strings like "anthropic/claude-opus-4-7"
    # or "openrouter/...". We pass them through as-is. For nop / oracle
    # agents the model is "default"; the CLI doesn't need -m for those.
    cmd = ODDISH_BIN + [
        "run",
        "--task", task_id,
        "--experiment", experiment_id,
        "-a", agent,
        "--n-trials", str(n),
        "-b",
        "-q",
    ]
    if model and model != "default":
        cmd += ["-m", model]
    env = dict(os.environ)
    env.setdefault("ODDISH_API_KEY", "ok_0b6748e0644c17e23319900ac4aff79c")
    env["PATH"] = os.path.expanduser("~/.local/bin") + os.pathsep + env.get("PATH", "")
    log(f"  -> RUN {' '.join(shlex.quote(c) for c in cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            cwd=ODDISH_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        log("     TIMEOUT calling oddish run")
        return False
    if proc.returncode != 0:
        tail_err = (proc.stderr or "").strip().splitlines()[-3:]
        tail_out = (proc.stdout or "").strip().splitlines()[-3:]
        log(f"     FAILED rc={proc.returncode}: stderr={tail_err} stdout={tail_out}")
        return False
    return True


async def run_iteration(iteration: int) -> dict:
    pairs = await fetch_pair_state()
    summary = {"in_scope": 0, "ok": 0, "in_flight": 0, "kicked": 0, "kick_targets": []}

    by_exp: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        if p["task_id"] in SKIP_TASKS:
            continue
        by_exp[p["experiment_id"]].append(p)

    for exp in EXPERIMENTS:
        rows = by_exp.get(exp, [])
        log(f"--- iter {iteration} :: exp {exp} :: {len(rows)} pairs in scope ---")
        for p in rows:
            summary["in_scope"] += 1
            S = p["success"]
            R = p["active"]
            F = p["failed"]
            label = (
                f"{p['task_id']:45s} {p['agent']:14s} {p['model'][:45]:45s}"
                f" S={S} R={R} F={F}"
            )
            deficit = TARGET_VALID - S - R
            if S >= TARGET_VALID:
                summary["ok"] += 1
                continue
            if deficit <= 0:
                summary["in_flight"] += 1
                log(f"  ~ {label} -> waiting on {R} active")
                continue
            n = min(deficit, PER_PAIR_MAX_PER_ITER)
            log(f"  ! {label} -> kicking {n} (deficit={deficit})")
            ok = kick_off_trials(exp, p["task_id"], p["agent"], p["model"], n)
            if ok:
                summary["kicked"] += n
                summary["kick_targets"].append(
                    f"{exp}/{p['task_id']}/{p['agent']}/{p['model']} +{n}"
                )

    return summary


async def main_loop() -> None:
    iteration = 0
    while True:
        iteration += 1
        try:
            summary = await run_iteration(iteration)
            log(
                f"== iter {iteration} done :: in_scope={summary['in_scope']} "
                f"ok={summary['ok']} in_flight={summary['in_flight']} "
                f"kicked={summary['kicked']}"
            )
            if summary["kick_targets"]:
                for t in summary["kick_targets"]:
                    log(f"   kicked: {t}")
        except Exception as e:  # noqa: BLE001
            log(f"!! iteration error: {e!r}")
        log(f"-- sleeping {SLEEP_SECONDS}s --")
        time.sleep(SLEEP_SECONDS)


if __name__ == "__main__":
    asyncio.run(main_loop())
