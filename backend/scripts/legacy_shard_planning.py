"""Read-only: task-group size distribution, to plan sharding for the full run.

ONE-OFF MIGRATION SCRIPT -- TEMPORARY BY DESIGN. Every statement is a SELECT.

Why this matters: legacy_transfer runs task-groups CONCURRENTLY but the trials
inside a group STRICTLY SEQUENTIALLY (run-order index + the importer's per-task
lock). Two consequences:

  * A base's usable parallelism is capped by its GROUP COUNT. Sharding across
    containers only helps if there are many groups.
  * The LARGEST GROUP sets a hard floor on wall-clock for that base, no matter
    how many containers you throw at it. If one group exceeds the 6h Modal
    timeout on its own, sharding cannot fix it -- that needs intra-task
    parallelism or splitting the group.

Measured baseline used for the floor estimate: ~1.08 trials/s sequential inside
a single group (prod, pr-509 pilot re-run).

Usage:
    modal run backend/scripts/legacy_shard_planning.py
"""

import os

import modal

app = modal.App("oddish-legacy-shard-planning")
image = modal.Image.debian_slim(python_version="3.13").pip_install("asyncpg")
secret = modal.Secret.from_name("oddish-prod", environment_name="main")

# Sequential within-group rate measured on prod (trials/sec).
SEQ_RATE = 1.08
MODAL_TIMEOUT_H = 6.0


@app.function(image=image, secrets=[secret], timeout=600)
async def plan() -> bool:
    import asyncpg

    url = os.environ["ODDISH_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url, statement_cache_size=0)

    # Remaining work only: already-transferred rows are not part of the plan.
    remaining = "status IN ('discovered','failed')"

    print("== Remaining work by base: group-size distribution ==")
    rows = await conn.fetch(
        f"""
        WITH g AS (
            SELECT s3_base, task_id, count(*) AS n
            FROM leg_trial_ledger
            WHERE {remaining}
            GROUP BY s3_base, task_id
        )
        SELECT s3_base,
               sum(n)::bigint                                        AS trials,
               count(*)::bigint                                      AS groups,
               round(avg(n), 1)                                      AS avg_size,
               percentile_cont(0.5)  WITHIN GROUP (ORDER BY n)       AS p50,
               percentile_cont(0.9)  WITHIN GROUP (ORDER BY n)       AS p90,
               percentile_cont(0.99) WITHIN GROUP (ORDER BY n)       AS p99,
               max(n)::bigint                                        AS max_size
        FROM g
        GROUP BY s3_base
        ORDER BY sum(n) DESC
        """
    )

    hdr = f"  {'base':32} {'trials':>9} {'groups':>8} {'avg':>7} {'p50':>6} {'p90':>7} {'p99':>8} {'max':>8} {'floor_h':>8}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    tot_trials = tot_groups = 0
    worst = []
    for r in rows:
        floor_h = float(r["max_size"]) / SEQ_RATE / 3600.0
        tot_trials += int(r["trials"])
        tot_groups += int(r["groups"])
        flag = "  <-- EXCEEDS TIMEOUT" if floor_h > MODAL_TIMEOUT_H else ""
        if floor_h > MODAL_TIMEOUT_H:
            worst.append((r["s3_base"], int(r["max_size"]), floor_h))
        print(
            f"  {r['s3_base'][:32]:32} {r['trials']:>9} {r['groups']:>8} "
            f"{float(r['avg_size']):>7.1f} {float(r['p50']):>6.0f} {float(r['p90']):>7.0f} "
            f"{float(r['p99']):>8.0f} {r['max_size']:>8} {floor_h:>8.2f}{flag}"
        )
    print(f"\n  TOTAL remaining: {tot_trials} trials in {tot_groups} groups")

    # The individual groups that would dominate any schedule.
    print("\n== 20 largest remaining task-groups (these set the tail) ==")
    big = await conn.fetch(
        f"""
        SELECT s3_base, task_id, count(*) AS n
        FROM leg_trial_ledger
        WHERE {remaining}
        GROUP BY s3_base, task_id
        ORDER BY count(*) DESC
        LIMIT 20
        """
    )
    for b in big:
        h = float(b["n"]) / SEQ_RATE / 3600.0
        print(f"  {b['s3_base'][:30]:30} {str(b['task_id'])[:38]:38} n={b['n']:>7}  ~{h:.2f}h sequential")

    # Concentration: how much of the work sits in the biggest groups.
    print("\n== Concentration (how top-heavy is the work?) ==")
    conc = await conn.fetch(
        f"""
        WITH g AS (
            SELECT count(*) AS n
            FROM leg_trial_ledger
            WHERE {remaining}
            GROUP BY s3_base, task_id
        ), ranked AS (
            SELECT n, row_number() OVER (ORDER BY n DESC) AS rk, count(*) OVER () AS total_groups,
                   sum(n) OVER () AS total_trials
            FROM g
        )
        SELECT
          max(total_groups)                                              AS groups,
          max(total_trials)                                              AS trials,
          sum(n) FILTER (WHERE rk <= greatest(total_groups/100, 1))      AS top1pct_trials,
          sum(n) FILTER (WHERE rk <= greatest(total_groups/10, 1))       AS top10pct_trials
        FROM ranked
        """
    )
    for c in conc:
        t = float(c["trials"] or 1)
        print(f"  total groups={c['groups']}  trials={c['trials']}")
        print(f"  top 1% of groups hold  {c['top1pct_trials']}  ({100*float(c['top1pct_trials'] or 0)/t:.1f}% of trials)")
        print(f"  top 10% of groups hold {c['top10pct_trials']}  ({100*float(c['top10pct_trials'] or 0)/t:.1f}% of trials)")

    if worst:
        print("\n== BLOCKERS: single groups exceeding the 6h function timeout ==")
        for base, n, h in worst:
            print(f"  {base}: largest group {n} trials ~{h:.1f}h sequential")
        print("  Sharding cannot fix these -- they need intra-task parallelism or splitting.")
    else:
        print(f"\n  No single group exceeds the {MODAL_TIMEOUT_H}h timeout. Sharding is viable everywhere.")

    await conn.close()
    return True


@app.local_entrypoint()
def main() -> None:
    plan.remote()
