"""Read-only prod preflight for the Sauron -> Oddish migration.

ONE-OFF MIGRATION SCRIPT -- TEMPORARY BY DESIGN.
Committed as a record of how the Sauron -> Oddish migration was run, not as
general-purpose tooling. It hardcodes assumptions specific to that migration
(the leg_* ledger tables, the 'sauron-migration' tag, the abundant-ai org) and
should be deleted once the migration is finished and the ledger tables are
dropped. Do not build on it or import from it.

Answers, without needing the Supabase web UI:
  1. Did the alembic migration actually apply to prod? (columns exist)
  2. What is prod's current alembic head?
  3. Is the pilot slice still untransferred in the ledger?
  4. How many rows would a rollback delete? (count only, never deletes)

Usage:
    modal run backend/scripts/legacy_prod_check.py
    modal run backend/scripts/legacy_prod_check.py --scope-base abundant-ai/experiments --scope-pr 509

Every statement here is a SELECT. There is no write path in this file.
"""

import os

import modal

app = modal.App("oddish-legacy-prod-check")

# Slim image on purpose: we only need a DB driver, not the oddish package.
image = modal.Image.debian_slim(python_version="3.13").pip_install("asyncpg")

secret = modal.Secret.from_name("oddish-prod", environment_name="main")

TARGET_ORG = "8ebde5d0"  # live "Abundant" org - see migration notes
IMPORT_TAG = "sauron-migration"

EXPECTED_COLUMNS = {
    "tasks": ["imported_at"],
    "trials": ["imported_at", "orig_s3_src"],
    "experiments": ["imported_at", "orig_s3_src"],
}


@app.function(image=image, secrets=[secret], timeout=300)
async def check(scope_base: str | None, scope_pr: int | None, show_errors: bool) -> bool:
    import asyncpg

    url = os.environ["ODDISH_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url, statement_cache_size=0)

    failures: list[str] = []

    def check_one(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")
        if not ok:
            failures.append(name)

    def info(name: str, detail: str = "") -> None:
        print(f"[info] {name}  {detail}")

    # Prove we are pointed at prod and not something else.
    host = url.split("@")[-1].split("/")[0] if "@" in url else "?"
    info("db host", host)

    # ------------------------------------------------------------- Columns
    print("\n== Migration applied? ==")
    rows = await conn.fetch(
        """
        SELECT table_name, column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND column_name IN ('imported_at', 'orig_s3_src')
        ORDER BY table_name, column_name
        """
    )
    found = {(r["table_name"], r["column_name"]) for r in rows}
    for r in rows:
        info(
            f"{r['table_name']}.{r['column_name']}",
            f"{r['data_type']} nullable={r['is_nullable']}",
        )

    for table, cols in EXPECTED_COLUMNS.items():
        for col in cols:
            check_one(f"{table}.{col} exists", (table, col) in found)

    # All migration columns must be nullable - a NOT NULL would mean something
    # other than our additive migration created them.
    non_nullable = [
        f"{r['table_name']}.{r['column_name']}" for r in rows if r["is_nullable"] != "YES"
    ]
    check_one("all migration columns nullable", not non_nullable, ", ".join(non_nullable))

    # ------------------------------------------------------------- Alembic
    # There are TWO alembic dirs sharing this database, each with its own
    # version table (see oddish/alembic/env.py:63, backend/alembic/env.py:77).
    # Bare `alembic_version` is a stale legacy table - do not trust it.
    print("\n== Alembic heads (per-dir version tables) ==")
    version_tables = await conn.fetch(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name LIKE 'alembic_version%'
        ORDER BY table_name
        """
    )
    info("version tables present", ", ".join(t["table_name"] for t in version_tables) or "(none)")

    oddish_heads: list[str] = []
    for t in version_tables:
        name = t["table_name"]
        rows_v = await conn.fetch(f"SELECT version_num FROM {name}")  # noqa: S608 - name from catalog
        versions = [r["version_num"] for r in rows_v]
        info(f"  {name}", ", ".join(versions) or "(empty)")
        if name == "alembic_version_oddish":
            oddish_heads = versions

    check_one(
        "single head in alembic_version_oddish",
        len(oddish_heads) == 1,
        f"count={len(oddish_heads)}",
    )
    check_one(
        "oddish head is not the stale bare table",
        bool(oddish_heads),
        "alembic_version_oddish must exist and be populated",
    )

    # ------------------------------------------------------------- Ledger
    print("\n== Ledger state (prod) ==")
    where = "TRUE"
    args: list = []
    if scope_base:
        args.append(scope_base)
        where += f" AND s3_base = ${len(args)}"
    if scope_pr is not None:
        args.append(scope_pr)
        where += f" AND pr_number = ${len(args)}"

    try:
        ledger = await conn.fetch(
            f"SELECT status, count(*) AS n FROM leg_trial_ledger WHERE {where} GROUP BY status ORDER BY 2 DESC",
            *args,
        )
        if not ledger:
            info("ledger rows in scope", "0 - check --scope-base / --scope-pr spelling")
        for r in ledger:
            info(f"  status={r['status']}", str(r["n"]))

        if scope_base or scope_pr is not None:
            total = sum(r["n"] for r in ledger)
            discovered = sum(r["n"] for r in ledger if r["status"] == "discovered")
            check_one(
                "pilot slice untransferred (safe to run)",
                total > 0 and discovered == total,
                f"discovered={discovered}/{total}",
            )
    except Exception as exc:  # noqa: BLE001
        check_one("ledger readable", False, repr(exc))

    # ------------------------------------------------------- Failure detail
    # The ledger stores each failure's reason in `error`, but nothing surfaced
    # it -- the runbook said "read the error column" with no way to do so.
    # Group by reason so a systemic cause is obvious versus scattered
    # one-offs.
    if show_errors:
        print("\n== Failed ledger rows, grouped by reason ==")
        try:
            errs = await conn.fetch(
                f"""
                SELECT coalesce(error, '(no reason recorded)') AS reason,
                       count(*) AS n,
                       min(s3_prefix) AS example
                FROM leg_trial_ledger
                WHERE status = 'failed' AND {where}
                GROUP BY 1 ORDER BY 2 DESC LIMIT 20
                """,
                *args,
            )
            if not errs:
                print("  (no failed rows in scope)")
            for e in errs:
                print(f"  n={e['n']:<6} {e['reason'][:150]}")
                print(f"           e.g. {e['example']}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [error] {exc!r}")

    # -------------------------------------------------- Rollback dry count
    print("\n== Rollback preview (COUNT ONLY - deletes nothing) ==")
    tag_where = "harbor_config->>'source' = $1"
    targs: list = [IMPORT_TAG]
    if scope_base:
        targs.append(scope_base)
        tag_where += f" AND harbor_config->>'legacy_s3_base' = ${len(targs)}"
    if scope_pr is not None:
        targs.append(str(scope_pr))
        tag_where += f" AND harbor_config->>'legacy_pr_number' = ${len(targs)}"

    try:
        n_trials = await conn.fetchval(f"SELECT count(*) FROM trials WHERE {tag_where}", *targs)
        info("imported trials in scope (would be deleted by rollback)", str(n_trials))

        n_all_imported = await conn.fetchval(
            "SELECT count(*) FROM trials WHERE imported_at IS NOT NULL"
        )
        info("ALL trials with imported_at set, org-wide", str(n_all_imported))
        info(
            "note",
            "if that org-wide number is non-zero before your pilot, a timestamp-only "
            "rollback would over-delete - scope any DELETE by harbor_config source too",
        )
    except Exception as exc:  # noqa: BLE001
        check_one("imported-trial count readable", False, repr(exc))

    await conn.close()

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return False
    print("ALL CHECKS PASSED - prod is ready for the pilot transfer.")
    return True


@app.local_entrypoint()
def main(scope_base: str | None = None, scope_pr: int | None = None,
         show_errors: bool = False) -> None:
    ok = check.remote(scope_base=scope_base, scope_pr=scope_pr, show_errors=show_errors)
    if not ok:
        raise SystemExit(1)
