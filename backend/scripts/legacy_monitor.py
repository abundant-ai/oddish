"""Live progress + health monitor for the migration.

ONE-OFF MIGRATION SCRIPT -- TEMPORARY BY DESIGN. Every statement is a SELECT.

Reads the LEDGER, not the shard logs, so it works regardless of how many shards
are running, survives a shard dying, and gives one number for the whole job.

Each tick prints: counts by ledger status, trials/s measured over the interval,
ETA, and DB health (active connections + lock waits). Health matters as much as
progress here: the failure mode we actually hit is asyncpg TimeoutError from too
much concurrent work against the Supabase transaction pooler, and that shows up
as rising lock waits / active connections before it shows up as dead shards.

Usage:
    modal run backend/scripts/legacy_monitor.py --scope-base abundant-ai/nov-5-export
    modal run backend/scripts/legacy_monitor.py                      # whole migration
    modal run backend/scripts/legacy_monitor.py --interval 15 --minutes 120
"""

import os

import modal

app = modal.App("oddish-legacy-monitor")
image = modal.Image.debian_slim(python_version="3.13").pip_install("asyncpg")
secret = modal.Secret.from_name("oddish-prod", environment_name="main")

DONE_STATES = ("transferred", "skipped")


@app.function(image=image, secrets=[secret], timeout=60 * 60 * 8)
async def watch(scope_base: str | None, interval: int, minutes: int) -> bool:
    import asyncio
    from datetime import datetime, timezone

    import asyncpg

    url = os.environ["ODDISH_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url, statement_cache_size=0)

    where, args = "TRUE", []
    if scope_base:
        args.append(scope_base)
        where += f" AND s3_base = ${len(args)}"

    def now():
        return datetime.now(timezone.utc)

    async def snapshot() -> dict:
        rows = await conn.fetch(
            f"SELECT status, count(*) AS n FROM leg_trial_ledger WHERE {where} GROUP BY status",
            *args,
        )
        return {r["status"]: r["n"] for r in rows}

    scope_label = scope_base or "ALL BASES"
    print(f"monitoring: {scope_label}   interval={interval}s   for {minutes}min")
    print(f"{'time':8}  {'done':>9} {'left':>9} {'failed':>7}  {'rate/s':>7} {'eta':>9}  {'conns':>5} {'locks':>5}")
    print("-" * 78)

    prev_done, prev_t, prev_failed = None, None, 0
    deadline = now().timestamp() + minutes * 60
    # Smooth the rate a little; a single interval is noisy when shards stagger.
    recent: list[float] = []
    idle = 0

    while now().timestamp() < deadline:
        counts = await snapshot()
        done = sum(counts.get(s, 0) for s in DONE_STATES)
        failed = counts.get("failed", 0)
        left = counts.get("discovered", 0) + failed
        total = sum(counts.values()) or 1
        t = now().timestamp()

        rate = 0.0
        if prev_done is not None and t > prev_t:
            inst = (done - prev_done) / (t - prev_t)
            recent.append(inst)
            del recent[:-5]
            rate = sum(recent) / len(recent)

        if rate > 0.01:
            eta_s = left / rate
            eta = f"{eta_s/3600:.1f}h" if eta_s >= 3600 else f"{eta_s/60:.0f}m"
        else:
            eta = "--"

        health = await conn.fetchrow(
            """
            SELECT count(*) FILTER (WHERE state = 'active')            AS active,
                   count(*) FILTER (WHERE wait_event_type = 'Lock')    AS locks
            FROM pg_stat_activity WHERE datname = current_database()
            """
        )

        pct = 100.0 * done / total
        stamp = datetime.now().strftime("%H:%M:%S")
        warn = ""
        if health["locks"] > 5:
            warn += "  <-- LOCK WAITS"
        if prev_done is not None and failed > prev_failed:
            warn += f"  <-- +{failed - prev_failed} FAILED"

        print(f"{stamp:8}  {done:>9} {left:>9} {failed:>7}  {rate:>7.2f} {eta:>9}  "
              f"{health['active']:>5} {health['locks']:>5}   {pct:5.1f}%{warn}")

        # Exit on 'discovered' hitting zero, NOT on left==0. left includes
        # failed rows, which never clear by themselves -- only a --retry-failed
        # run picks them up. Waiting for left==0 means spinning forever at 100%.
        pending = counts.get("discovered", 0)
        if pending == 0:
            if failed:
                print(f"\nNo pending work left. {failed} row(s) FAILED -- sweep them with:")
                print(f"  modal run backend/scripts/legacy_transfer.py "
                      f"--scope-base {scope_base or '<base>'} --execute --retry-failed")
            else:
                print("\nALL WORK COMPLETE for this scope.")
            break

        # Nothing moving and nothing failing usually means every shard died.
        if prev_done is not None and done == prev_done:
            idle += 1
            if idle >= 3:
                print(f"\nNo progress for {idle} intervals with {pending} rows still "
                      f"pending -- shards are probably all dead. Check the shard logs.")
                break
        else:
            idle = 0

        prev_done, prev_t, prev_failed = done, t, failed
        await asyncio.sleep(interval)

    await conn.close()
    return True


@app.local_entrypoint()
def main(scope_base: str | None = None, interval: int = 20, minutes: int = 180) -> None:
    watch.remote(scope_base=scope_base, interval=interval, minutes=minutes)
