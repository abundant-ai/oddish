"""Roll back a legacy-migration chunk from prod.

ONE-OFF MIGRATION SCRIPT -- TEMPORARY BY DESIGN.
Committed as a record of how the Sauron -> Oddish migration was run, not as
general-purpose tooling. It deletes rows using markers specific to that
migration (imported_at + the 'sauron-migration' harbor_config tag) and should
be deleted once the migration is finished. Do not build on it or repurpose it
as a general delete tool.

Safety model, verified against legacy_transfer.py rather than assumed:

  * Tasks and experiments are GET-OR-CREATE (transfer.py:341, :309). When the
    row already exists the transfer mutates NOTHING on it - no imported_at, no
    tags, no run_analysis. So `imported_at IS NOT NULL` can only ever match a
    row the migration itself created. A native task that legacy trials merged
    into is invisible to this script.

  * Trials additionally carry harbor_config->>'source' = 'sauron-migration',
    so trials are matched on BOTH the marker and the tag.

  * A task/experiment is deleted ONLY if it has no trials left after the trial
    delete. That keeps a shared parent alive when you roll back one chunk of a
    multi-chunk run.

  * Everything runs in ONE transaction. A partial rollback is impossible.

Usage:
    modal run backend/scripts/legacy_prod_rollback.py --scope-base abundant-ai/experiments --scope-pr 509
    modal run backend/scripts/legacy_prod_rollback.py --scope-base abundant-ai/experiments --scope-pr 509 --execute
"""

import os

import modal

app = modal.App("oddish-legacy-rollback")
image = modal.Image.debian_slim(python_version="3.13").pip_install("asyncpg")
secret = modal.Secret.from_name("oddish-prod", environment_name="main")

IMPORT_TAG = "sauron-migration"
LOCK_TIMEOUT = "5s"


@app.function(image=image, secrets=[secret], timeout=1800)
async def rollback(scope_base: str | None, scope_pr: int | None, execute: bool) -> bool:
    import asyncpg

    url = os.environ["ODDISH_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url, statement_cache_size=0)

    host = url.split("@")[-1].split("/")[0] if "@" in url else "?"
    print(f"[info] db host   {host}")
    print(f"[info] mode      {'EXECUTE' if execute else 'DRY RUN (no writes)'}")
    print(f"[info] scope     base={scope_base} pr={scope_pr}")

    if scope_base is None and scope_pr is None:
        print("\nREFUSING: no scope given. That would roll back EVERY imported row.")
        print("Pass --scope-base and/or --scope-pr.")
        await conn.close()
        return False

    # ---- who are we deleting -------------------------------------------------
    where = "imported_at IS NOT NULL AND harbor_config->>'source' = $1"
    args: list = [IMPORT_TAG]
    if scope_base:
        args.append(scope_base)
        where += f" AND harbor_config->>'legacy_s3_base' = ${len(args)}"
    if scope_pr is not None:
        args.append(str(scope_pr))
        where += f" AND harbor_config->>'legacy_pr_number' = ${len(args)}"

    # FK graph first, so a pre-pilot dry run still tells you whether the delete
    # order below is complete. This is the part you do NOT want to discover
    # for the first time while actually rolling something back.
    print("\n== FK dependents (catalog introspection) ==")
    fks = await conn.fetch(
        """
        SELECT c.conname, src.relname AS src_table, tgt.relname AS tgt_table,
               c.confdeltype
        FROM pg_constraint c
        JOIN pg_class src ON src.oid = c.conrelid
        JOIN pg_class tgt ON tgt.oid = c.confrelid
        WHERE c.contype = 'f' AND tgt.relname IN ('trials','tasks','experiments')
        ORDER BY tgt.relname, src.relname
        """
    )
    handled = {"trials", "tasks", "experiments", "task_versions"}
    cascade = {"c": "CASCADE", "n": "SET NULL", "a": "NO ACTION", "r": "RESTRICT", "d": "SET DEFAULT"}
    unhandled = set()
    for f in fks:
        # pg returns confdeltype as a "char" -> asyncpg hands it back as bytes.
        raw = f["confdeltype"]
        raw = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        ondel = cascade.get(raw, raw)
        safe = f["src_table"] in handled or ondel in ("CASCADE", "SET NULL")
        print(f"  [{'ok' if safe else '!!'}] {f['src_table']:24} -> {f['tgt_table']:12} on delete {ondel}")
        if not safe:
            unhandled.add(f["src_table"])
    if unhandled:
        print(f"\n  WARNING: no cascade and not handled: {', '.join(sorted(unhandled))}")
        print("  The delete would raise on these, rolling the whole transaction back safely,")
        print("  but rollback would not complete until they are handled explicitly.")
    else:
        print("\n  All dependents either cascade, null out, or are deleted explicitly.")

    trial_ids = [r["id"] for r in await conn.fetch(f"SELECT id FROM trials WHERE {where}", *args)]
    print(f"\n[info] trials in scope            {len(trial_ids)}")

    if not trial_ids:
        print("\nNothing to roll back in this scope (nothing imported yet).")
        await conn.close()
        return True

    # Parents these trials belong to, restricted to migration-created rows.
    parent_tasks = [
        r["id"]
        for r in await conn.fetch(
            "SELECT DISTINCT t.id FROM tasks t JOIN trials tr ON tr.task_id = t.id "
            "WHERE tr.id = ANY($1) AND t.imported_at IS NOT NULL",
            trial_ids,
        )
    ]
    parent_exps = [
        r["id"]
        for r in await conn.fetch(
            "SELECT DISTINCT e.id FROM experiments e JOIN trials tr ON tr.experiment_id = e.id "
            "WHERE tr.id = ANY($1) AND e.imported_at IS NOT NULL",
            trial_ids,
        )
    ]
    native_parents = await conn.fetchval(
        "SELECT count(DISTINCT t.id) FROM tasks t JOIN trials tr ON tr.task_id = t.id "
        "WHERE tr.id = ANY($1) AND t.imported_at IS NULL",
        trial_ids,
    )
    print(f"[info] migration-created tasks     {len(parent_tasks)}")
    print(f"[info] migration-created exps      {len(parent_exps)}")
    print(f"[info] NATIVE tasks merged into    {native_parents}  (never touched)")


    # worker_jobs uses subject_table/subject_id, not a real FK - check by hand.
    try:
        jobs = await conn.fetchval(
            "SELECT count(*) FROM worker_jobs WHERE (subject_table='trials' AND subject_id = ANY($1)) "
            "OR (subject_table='tasks' AND subject_id = ANY($2))",
            trial_ids,
            parent_tasks,
        )
        print(f"\n[info] worker_jobs referencing these rows  {jobs}")
        if jobs:
            print("  (expected 0 - imported trials never enqueue. Investigate if non-zero.)")
    except Exception as exc:  # noqa: BLE001
        print(f"\n[info] worker_jobs check skipped: {exc!r}")

    if not execute:
        print("\n" + "=" * 60)
        print("DRY RUN. Would delete, in one transaction:")
        print(f"  {len(trial_ids)} trials")
        print(f"  up to {len(parent_tasks)} tasks + {len(parent_exps)} experiments (only if left empty)")
        print(f"  and reset {len(trial_ids)} ledger rows to 'discovered'")
        print("\nRe-run with --execute.")
        await conn.close()
        return True

    # ---- the delete ----------------------------------------------------------
    await conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
    async with conn.transaction():
        d_trials = await conn.execute("DELETE FROM trials WHERE id = ANY($1)", trial_ids)
        print(f"\n[ok]   trials         {d_trials}")

        # Only drop a parent that is now childless - a shared parent survives.
        empty_tasks = [
            r["id"]
            for r in await conn.fetch(
                "SELECT t.id FROM tasks t WHERE t.id = ANY($1) "
                "AND NOT EXISTS (SELECT 1 FROM trials tr WHERE tr.task_id = t.id)",
                parent_tasks,
            )
        ]
        empty_exps = [
            r["id"]
            for r in await conn.fetch(
                "SELECT e.id FROM experiments e WHERE e.id = ANY($1) "
                "AND NOT EXISTS (SELECT 1 FROM trials tr WHERE tr.experiment_id = e.id)",
                parent_exps,
            )
        ]

        if empty_tasks:
            # current_version_id -> task_versions is self-referential; break it first.
            await conn.execute(
                "UPDATE tasks SET current_version_id = NULL WHERE id = ANY($1)", empty_tasks
            )
            d_vers = await conn.execute(
                "DELETE FROM task_versions WHERE task_id = ANY($1)", empty_tasks
            )
            print(f"[ok]   task_versions  {d_vers}")
            d_tasks = await conn.execute("DELETE FROM tasks WHERE id = ANY($1)", empty_tasks)
            print(f"[ok]   tasks          {d_tasks}")
        kept_tasks = len(parent_tasks) - len(empty_tasks)
        if kept_tasks:
            print(f"[info] kept {kept_tasks} task(s) - still have trials from another chunk")

        if empty_exps:
            d_exps = await conn.execute("DELETE FROM experiments WHERE id = ANY($1)", empty_exps)
            print(f"[ok]   experiments    {d_exps}")

        # Put the ledger back so the chunk is re-runnable.
        led_where = "TRUE"
        led_args: list = []
        if scope_base:
            led_args.append(scope_base)
            led_where += f" AND s3_base = ${len(led_args)}"
        if scope_pr is not None:
            led_args.append(scope_pr)
            led_where += f" AND pr_number = ${len(led_args)}"
        d_led = await conn.execute(
            f"UPDATE leg_trial_ledger SET status='discovered', error=NULL "  # noqa: S608
            f"WHERE {led_where} AND status IN ('transferred','failed','transferring')",
            *led_args,
        )
        print(f"[ok]   ledger reset   {d_led}")

    left = await conn.fetchval(f"SELECT count(*) FROM trials WHERE {where}", *args)
    await conn.close()

    print("\n" + "=" * 60)
    ok = left == 0
    print(f"{'DONE' if ok else 'INCOMPLETE'} - {left} tagged trials remain in scope")
    return ok


@app.local_entrypoint()
def main(scope_base: str | None = None, scope_pr: int | None = None, execute: bool = False) -> None:
    ok = rollback.remote(scope_base=scope_base, scope_pr=scope_pr, execute=execute)
    if not ok:
        raise SystemExit(1)
