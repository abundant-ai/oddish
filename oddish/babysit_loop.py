"""Autonomous babysitter loop for experiment d8d31fa9.

Tops up each (task, agent) cell to TARGET valid trials, respecting a per-agent
in-flight cap and a per-cell total-attempt cap. Polls forever; logs every
decision. Aborts on auth/402-style errors in finished trials.
"""
import asyncio
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import text
from oddish.db import get_session, init_db
from oddish.core.endpoints import create_task_sweep_core
from oddish.schemas import (
    TaskSweepSubmission, AgentModelPair, HarborConfig, HarborAgentConfig,
)
from harbor.models.environment_type import EnvironmentType

ORG = "8ebde5d0"
EXP = "d8d31fa9"
ALLOWED = frozenset({EnvironmentType.MODAL, EnvironmentType.DAYTONA})

TASK_IDS = [
    "biofabric-rust-rewrite-897c0fc8", "embedding-eval-be1dc228", "excel-clone-685bff89",
    "find-network-alignments-b2ecebb7", "jax-pytorch-rewrite-93c38683", "kubernetes-rust-rewrite-bae0f616",
    "mastodon-clone-e9964b7a", "nextjs-vite-rewrite-b0d028b0", "parameter-golf-61e11605",
    "post-train-ifeval-c9f2cfbc", "ruby-rust-port-383776ff", "rust-c-compiler-57913cbe",
    "rust-java-lsp-0a9c67d6", "s3-clone-23996371", "slack-clone-b0a98beb", "stripe-clone-66061f4d",
    "trimul-cuda-fc34aaba", "vliw-kernel-optimization-e4d25121", "wasm-simd-40e18127", "zstd-decoder-72bbfded",
]
AGENTS = [
    ("minimax-claude-code", "minimax/MiniMax-M3"),
    ("kimi-claude-code", "moonshot/kimi-k2.7-code"),
]
AGENT_NAMES = [a for a, _ in AGENTS]

TARGET = 3
CAP_INFLIGHT = 12          # max queued+running per agent
PENDING_CAP = 2            # max simultaneous in-flight attempts per cell
MAX_TOTAL_PER_CELL = 8     # safety: stop retrying a cell after this many trials
# Stricter per-cell caps for known-unreliable tasks (per handoff notes).
CELL_CAP_OVERRIDE = {
    "embedding-eval-be1dc228": 6,
    "post-train-ifeval-c9f2cfbc": 7,
    "nextjs-vite-rewrite-b0d028b0": 7,
    "kubernetes-rust-rewrite-bae0f616": 7,
    "trimul-cuda-fc34aaba": 8,
}
POLL_SECONDS = 240

PENDING_STATES = {"queued", "running", "retrying", "pending"}
# NOTE: error_message embeds the full task prompt + command, so substring
# scanning for auth patterns is unreliable (task specs contain "401",
# "authentication", etc.). Genuine provider auth/402 failures are checked
# separately via babysit_authcheck.py (inspects agent logs). The loop just
# requeues infra failures (exit 137 / -1 / network) like any other invalid.


def log(*a):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}]", *a, flush=True)


def is_valid(status, reward, output_tokens, error_message):
    em = (error_message or "").lower()
    if status == "success" and reward is not None and (output_tokens or 0) >= 2000:
        return True
    if status == "failed" and ("timed out" in em or "agenttimeout" in em):
        return True
    return False


async def fetch_state(session):
    q = text(
        """
        SELECT t.id, t.agent, t.task_id, t.status, t.reward,
               t.output_tokens, t.error_message, t.created_at
        FROM trials t
        WHERE t.experiment_id = :exp AND t.deleted_at IS NULL
        """
    )
    rows = (await session.execute(q, {"exp": EXP})).mappings().all()
    valid = defaultdict(int)
    pending = defaultdict(int)
    invalid = defaultdict(int)
    total = defaultdict(int)
    inflight_agent = defaultdict(int)
    for r in rows:
        st = (r["status"] or "").lower()
        ag = r["agent"]
        tid = r["task_id"]
        key = (tid, ag)
        if tid not in TASK_IDS or ag not in AGENT_NAMES:
            continue
        total[key] += 1
        if is_valid(st, r["reward"], r["output_tokens"], r["error_message"]):
            valid[key] += 1
        elif st in PENDING_STATES:
            pending[key] += 1
            inflight_agent[ag] += 1
        else:
            invalid[key] += 1
    return valid, pending, invalid, total, inflight_agent


def cfg(agent, model):
    # Restore the validated GLM-parity agent_config. The persistent prod
    # config is missing CLAUDE_STREAM_IDLE_TIMEOUT_MS / EAGER_FLUSH /
    # MAX_OUTPUT_TOKENS, which lets MiniMax bail with a single long thinking
    # turn (0 output tokens) on heavy reasoning tasks like trimul-cuda.
    # Harmless if redundant per handoff notes.
    if agent == "minimax-claude-code":
        return HarborAgentConfig(
            name=agent, model_name=model,
            env={
                "CLAUDE_STREAM_IDLE_TIMEOUT_MS": "3600000",
                "CLAUDE_CODE_EAGER_FLUSH": "1",
                "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "128000",
                "API_TIMEOUT_MS": "3600000",
            },
            kwargs={"version": "2.1.167"},
        )
    if agent == "kimi-claude-code":
        return HarborAgentConfig(
            name=agent, model_name=model,
            kwargs={
                "disallowed_tools": "WebSearch WebFetch EnterPlanMode ExitPlanMode AskUserQuestion",
                "version": "2.1.167",
            },
        )
    return None


async def submit(session, task_id, agent, model, n):
    sub = TaskSweepSubmission(
        task_id=task_id, experiment_id=EXP,
        configs=[AgentModelPair(agent=agent, model=model, n_trials=n,
                                agent_config=cfg(agent, model))],
        environment=EnvironmentType.MODAL, user="minimax+kimi babysitter",
        max_trial_attempts=1, harbor=HarborConfig(),
    )
    _, trials, _, _ = await create_task_sweep_core(
        session, submission=sub, org_id=ORG,
        default_environment=EnvironmentType.MODAL, allowed_environments=ALLOWED,
    )
    await session.commit()
    return [t.id for t in trials]


def cell_cap(task_id):
    return CELL_CAP_OVERRIDE.get(task_id, MAX_TOTAL_PER_CELL)


async def one_round():
    async with get_session() as s:
        valid, pending, invalid, total, inflight = await fetch_state(s)

    # progress summary
    cells_done = sum(1 for t in TASK_IDS for a in AGENT_NAMES if valid[(t, a)] >= TARGET)
    cells_1 = sum(1 for t in TASK_IDS for a in AGENT_NAMES if valid[(t, a)] >= 1)
    log(f"valid>={TARGET}: {cells_done}/40 | valid>=1: {cells_1}/40 | "
        f"inflight minimax={inflight['minimax-claude-code']} kimi={inflight['kimi-claude-code']}")

    submitted_any = False
    for agent, model in AGENTS:
        budget = CAP_INFLIGHT - inflight[agent]
        if budget <= 0:
            continue
        # candidate cells: need = TARGET - valid - pending, not over cap
        cands = []
        for task in TASK_IDS:
            key = (task, agent)
            need = TARGET - valid[key] - pending[key]
            room = cell_cap(task) - total[key]
            inflight_room = PENDING_CAP - pending[key]
            eff = max(0, min(need, room, inflight_room))
            if eff > 0:
                cands.append((valid[key], pending[key], task, eff))
        if not cands:
            continue
        # 1) cells at 0 valid first (guarantee coverage), then
        # 2) greedy completion: deepen highest-valid cells first so v2->v3
        #    cells finish and exit the pool instead of uniform breadth.
        cands.sort(key=lambda c: (0 if c[0] == 0 else 1, -c[0], c[1]))
        # round-robin one trial per cell until budget exhausted
        idx = 0
        local_pending = dict()
        plan = defaultdict(int)
        remaining = {(c[2]): c[3] for c in cands}
        order = [c[2] for c in cands]
        while budget > 0 and any(remaining[t] > 0 for t in order):
            task = order[idx % len(order)]
            if remaining[task] > 0:
                plan[task] += 1
                remaining[task] -= 1
                budget -= 1
            idx += 1
        # execute plan
        for task, n in plan.items():
            try:
                async with get_session() as s:
                    ids = await submit(s, task, agent, model, n)
                log(f"  +{n} {agent[:7]} {task} -> {ids}")
                submitted_any = True
            except Exception as e:
                log(f"  !! submit failed {agent[:7]} {task}: {type(e).__name__}: {e}")
    if not submitted_any:
        log("  (no submissions this round)")
    return "OK"


async def main():
    await init_db()
    once = "--once" in sys.argv
    log(f"Babysitter start. TARGET={TARGET} CAP_INFLIGHT={CAP_INFLIGHT} "
        f"MAX_TOTAL_PER_CELL={MAX_TOTAL_PER_CELL} POLL={POLL_SECONDS}s once={once}")
    round_no = 0
    while True:
        round_no += 1
        log(f"--- round {round_no} ---")
        try:
            res = await one_round()
        except Exception as e:
            log(f"round error: {type(e).__name__}: {e}")
            res = "OK"
        if res == "ABORT":
            log("Loop aborted. Exiting.")
            sys.exit(2)
        if once:
            log("--once complete, exiting.")
            return
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
