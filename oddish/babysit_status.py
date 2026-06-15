import asyncio
from collections import defaultdict
from sqlalchemy import text
from oddish.db import get_session, init_db

EXP = "d8d31fa9"
ORG = "8ebde5d0"

TASK_IDS = [
    "biofabric-rust-rewrite-897c0fc8", "embedding-eval-be1dc228", "excel-clone-685bff89",
    "find-network-alignments-b2ecebb7", "jax-pytorch-rewrite-93c38683", "kubernetes-rust-rewrite-bae0f616",
    "mastodon-clone-e9964b7a", "nextjs-vite-rewrite-b0d028b0", "parameter-golf-61e11605",
    "post-train-ifeval-c9f2cfbc", "ruby-rust-port-383776ff", "rust-c-compiler-57913cbe",
    "rust-java-lsp-0a9c67d6", "s3-clone-23996371", "slack-clone-b0a98beb", "stripe-clone-66061f4d",
    "trimul-cuda-fc34aaba", "vliw-kernel-optimization-e4d25121", "wasm-simd-40e18127", "zstd-decoder-72bbfded",
]
AGENTS = ["minimax-claude-code", "kimi-claude-code"]

PENDING_STATES = {"queued", "running", "retrying", "pending"}


def is_valid(status, reward, output_tokens, error_message):
    em = (error_message or "").lower()
    if status == "success" and reward is not None and (output_tokens or 0) >= 2000:
        return True
    if status == "failed" and ("timed out" in em or "agenttimeout" in em):
        return True
    return False


def is_invalid_finished(status, reward, output_tokens, error_message):
    # finished (not pending) and not valid
    if status in PENDING_STATES:
        return False
    return not is_valid(status, reward, output_tokens, error_message)


async def main():
    await init_db()
    q = text(
        """
        SELECT t.id, t.agent, t.task_id, k.name AS task_name, t.status, t.reward,
               t.output_tokens, t.error_message
        FROM trials t JOIN tasks k ON k.id = t.task_id
        WHERE t.experiment_id = :exp AND t.deleted_at IS NULL
        """
    )
    async with get_session() as s:
        rows = (await s.execute(q, {"exp": EXP})).mappings().all()

    valid = defaultdict(int)
    pending = defaultdict(int)
    invalid = defaultdict(int)
    other_tasks = defaultdict(int)
    by_status = defaultdict(int)

    for r in rows:
        key = (r["task_id"], r["agent"])
        st = (r["status"] or "").lower()
        by_status[(r["agent"], st)] += 1
        if r["task_id"] not in TASK_IDS:
            other_tasks[r["task_id"]] += 1
        if is_valid(st, r["reward"], r["output_tokens"], r["error_message"]):
            valid[key] += 1
        elif st in PENDING_STATES:
            pending[key] += 1
        else:
            invalid[key] += 1

    print(f"Total trials in {EXP}: {len(rows)}")
    print("\nAgents present:", sorted({r['agent'] for r in rows}))
    print("\nStatus counts by agent:")
    for (ag, st), c in sorted(by_status.items()):
        print(f"  {ag:24} {st:10} {c}")

    print("\n=== Per-cell matrix (valid / pending / invalid) ===")
    header = f"{'task':40}"
    for ag in AGENTS:
        short = "minimax" if "minimax" in ag else "kimi"
        header += f" | {short:>18}"
    print(header)
    short_names = {ag: ("minimax" if "minimax" in ag else "kimi") for ag in AGENTS}
    n_meet1 = 0
    n_meet3 = 0
    total_cells = len(TASK_IDS) * len(AGENTS)
    for task in TASK_IDS:
        line = f"{task:40}"
        for ag in AGENTS:
            key = (task, ag)
            v, p, iv = valid[key], pending[key], invalid[key]
            line += f" | v{v:>2} p{p:>2} x{iv:>2}    "
            if v >= 1:
                n_meet1 += 1
            if v >= 3:
                n_meet3 += 1
        print(line)

    print(f"\nCells with >=1 valid: {n_meet1}/{total_cells}")
    print(f"Cells with >=3 valid: {n_meet3}/{total_cells}")

    if other_tasks:
        print("\nWARNING: trials on tasks not in the 20-task list:")
        for t, c in other_tasks.items():
            print(f"  {t}: {c}")


if __name__ == "__main__":
    asyncio.run(main())
