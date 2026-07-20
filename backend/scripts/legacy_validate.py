"""Objective validation of the Sauron->Oddish import.

Runs FIVE mechanical layers against imported rows (no human judgement needed).
The reference standard is the existing native data (``origin='oddish'``) that
already renders in prod: imported rows must be shape-identical to it, faithful
to their S3 source, referentially complete, and accepted by the same endpoints.

Scope it to the pilot slice with --scope-base / --scope-run, or run over all
imports. Only rows tagged ``harbor_config->>'source' = 'sauron-migration'`` are
considered (this excludes the ~1,434 prior unrelated test imports).

Exit code is non-zero if any hard check fails, so it doubles as a gate.

Usage:
    modal run backend/scripts/legacy_validate.py                                   # all imports
    modal run backend/scripts/legacy_validate.py --scope-base abundant-ai/anthropic
    modal run backend/scripts/legacy_validate.py --scope-run 19205653200 --sample 100
    modal run backend/scripts/legacy_validate.py --scope-pr 509    # one legacy pr-N chunk

Scope flags mirror legacy_transfer.py so a chunk moved with a given scope can be
validated with the same scope. The production migration runs in chunks (by repo,
run, or legacy pr-N), and each chunk is validated in isolation with the matching
flag; without --scope-pr, a pr-sized chunk could not be checked on its own.
"""

from __future__ import annotations

from pathlib import Path

import modal

_parents = Path(__file__).resolve().parents
REPO = _parents[2] if len(_parents) > 2 else Path("/")

app = modal.App("oddish-legacy-validate")
# Harbor commit both oddish/uv.lock and backend/uv.lock resolve for the
# abundant-ai fork. Keep in sync with those lockfiles.
HARBOR_SHA = "555fc203d51ef97d937703654e7d03b29cba4a02"

image = (
    modal.Image.debian_slim(python_version="3.13")
    .apt_install("git")
    .add_local_dir(str(REPO / "oddish"), remote_path="/oddish", copy=True, ignore=[".venv/", ".git/"])
    .run_commands(
        "pip install uv",
        "cd /oddish && uv pip install --system '.[worker]'",
        # `uv pip install` is the pip-compatible interface: it applies
        # [tool.uv] override-dependencies (harbor==0.16.1) but IGNORES
        # [tool.uv.sources], so harbor resolves from PyPI instead of the
        # abundant-ai fork. PyPI's 0.16.1 has no harbor.environments.kube_ops,
        # which oddish/workers/harbor/runner.py imports at module scope -> the
        # job dies on `import oddish.queue`. The production worker image avoids
        # this by using uv_sync (project API, honours sources + lockfile); see
        # backend/modal_app.py::_build_worker_image. Reinstall harbor from the
        # fork at the exact commit both uv.lock files resolve, so this image
        # matches production rather than floating on branch main.
        f"uv pip install --system 'harbor @ git+https://github.com/abundant-ai/harbor@{HARBOR_SHA}'",
    )
)
secret = modal.Secret.from_name("oddish-prod", environment_name="main")
aws_secret = modal.Secret.from_name("sauron-legacy")  # legacy-bucket AWS keys

DEFAULT_BUCKET = "abundant-github-workflows-bucket"
IMPORT_TAG = "sauron-migration"

# Columns that native rows (almost) always populate and the UI relies on. If an
# imported row leaves one NULL where native never does, the page can break.
RENDER_CRITICAL = ["experiment_id", "task_id", "status", "provider", "queue_key", "origin"]


@app.function(image=image, secrets=[secret, aws_secret], timeout=3600)
async def validate(scope_base: str | None, scope_run: str | None,
                   scope_pr: int | None, sample: int) -> bool:
    import json

    import aioboto3
    import asyncpg
    from oddish.config import settings

    # litellm prints "Provider List: https://docs.litellm.ai/docs/providers" to
    # stdout every time it is asked about a model id it does not recognise --
    # which, for legacy Sauron model ids, is constantly. It produced hundreds of
    # lines per run and buried the actual output. This flag is litellm's
    # documented switch for that chatter and is process-local, so deployed
    # prod code is unaffected.
    try:
        import litellm
        litellm.suppress_debug_info = True
    except Exception:
        pass

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}  {detail}")
        if not ok:
            failures.append(name)

    def info(name: str, detail: str = "") -> None:
        print(f"[info] {name}  {detail}")

    conn = await asyncpg.connect(settings.asyncpg_url, statement_cache_size=0)

    # Build the "our imports" filter (tag + optional pilot scope).
    where = "harbor_config->>'source' = $1"
    args: list = [IMPORT_TAG]
    if scope_base:
        args.append(scope_base)
        where += f" AND harbor_config->>'legacy_s3_base' = ${len(args)}"
    if scope_run:
        args.append(scope_run)
        where += f" AND harbor_config->>'legacy_run_id' = ${len(args)}"
    if scope_pr is not None:
        # legacy_pr_number is stored as a JSON number; ->> extracts it as text.
        args.append(str(scope_pr))
        where += f" AND harbor_config->>'legacy_pr_number' = ${len(args)}"

    total = await conn.fetchval(f"SELECT count(*) FROM trials WHERE {where}", *args)
    check("imported trials present", total > 0, f"count={total}")
    if not total:
        await conn.close()
        print("\nNothing to validate (no rows carry the migration tag in this scope).")
        return False

    org_id = await conn.fetchval(f"SELECT org_id FROM trials WHERE {where} LIMIT 1", *args)
    info("target org_id", str(org_id))

    # ---------------------------------------------------------------- Layer 1
    print("\n== Layer 1: internal invariants ==")
    orphan_task = await conn.fetchval(
        f"SELECT count(*) FROM trials t WHERE {where} "
        "AND NOT EXISTS (SELECT 1 FROM tasks k WHERE k.id = t.task_id)", *args)
    check("every trial has a task", orphan_task == 0, f"orphans={orphan_task}")

    orphan_exp = await conn.fetchval(
        f"SELECT count(*) FROM trials t WHERE {where} "
        "AND NOT EXISTS (SELECT 1 FROM experiments e WHERE e.id = t.experiment_id)", *args)
    check("every trial has an experiment", orphan_exp == 0, f"orphans={orphan_exp}")

    dup_keys = await conn.fetchval(
        f"SELECT count(*) FROM (SELECT idempotency_key FROM trials WHERE {where} "
        "AND idempotency_key IS NOT NULL GROUP BY idempotency_key HAVING count(*) > 1) d", *args)
    check("no duplicate idempotency keys", dup_keys == 0, f"dups={dup_keys}")

    # Ledger completeness (HARD FAIL). Every trial in scope must be finished:
    # 'transferred' (moved in), or deliberately excluded as 'skipped' /
    # 'duplicate'. Both exclusion labels matter: the Sauron-mirror true-dups
    # (native Oddish trials written back into the legacy bucket) were first
    # marked 'skipped' and later relabelled 'duplicate', and this gate only
    # knew the old name -- so abundant-ai/experiments hard-failed with
    # UNFINISHED=duplicate:24292 even though 142,064 + 24,292 = 166,356
    # accounted for every single ledger row. Anything still 'discovered' (never attempted),
    # 'transferring' (stuck mid-move), or 'failed' (errored) means the move is
    # incomplete and data is silently missing. The ledger already records each
    # trial's state, so we read that column directly — unlike comparing two
    # totals, a leftover row cannot hide behind a coincidentally-matching count.
    # (Run the transfer with --retry-failed to clear 'failed' rows first.)
    #
    # NOTE (intentionally strict; do not "scope" this to the migration tag):
    # leg_trial_ledger IS the migration's own worklist — every row is a legacy
    # trial that must move. There are no foreign/untagged rows to filter out;
    # the only rows we deliberately don't move are the dedup mirrors, already
    # marked 'skipped'/'duplicate' and excluded below. So any in-scope row that
    # is not one of those three is a genuine un-moved trial and SHOULD fail the
    # gate. Validating a broader scope than you transferred will (correctly)
    # report the untouched remainder as unfinished — that is the point.
    lwhere = "TRUE"
    largs: list = []
    if scope_base:
        largs.append(scope_base); lwhere += f" AND s3_base = ${len(largs)}"
    if scope_run:
        largs.append(scope_run); lwhere += f" AND run_id = ${len(largs)}"
    if scope_pr is not None:
        largs.append(scope_pr); lwhere += f" AND pr_number = ${len(largs)}"
    ledger_n = await conn.fetchval(f"SELECT count(*) FROM leg_trial_ledger WHERE {lwhere}", *largs)
    unfinished = await conn.fetch(
        f"SELECT status, count(*) AS n FROM leg_trial_ledger "
        f"WHERE {lwhere} AND status NOT IN ('transferred','skipped','duplicate') "
        f"GROUP BY status ORDER BY status", *largs)
    n_unfinished = sum(r["n"] for r in unfinished)
    detail = f"ledger={ledger_n} imported={total}"
    if unfinished:
        detail += "  UNFINISHED=" + ",".join(f"{r['status']}:{r['n']}" for r in unfinished)
    check("every ledger trial is finished (moved or deliberately skipped)",
          n_unfinished == 0, detail)

    # ---------------------------------------------------------------- Layer 2
    print("\n== Layer 2: structural parity vs native (origin='oddish', same org) ==")
    # Render-critical columns must be 100% populated in imported rows.
    for col in RENDER_CRITICAL:
        nulls = await conn.fetchval(
            f"SELECT count(*) FROM trials WHERE {where} AND {col} IS NULL", *args)
        check(f"render-critical '{col}' never NULL", nulls == 0, f"nulls={nulls}")

    # Enum-ish columns: imported values must be a subset of native values.
    for col in ["status", "provider"]:
        imp = {r[0] for r in await conn.fetch(
            f"SELECT DISTINCT {col} FROM trials WHERE {where}", *args)}
        nat = {r[0] for r in await conn.fetch(
            f"SELECT DISTINCT {col} FROM trials WHERE origin='oddish' AND org_id=$1", org_id)}
        unknown = imp - nat
        check(f"'{col}' values are known to prod", not unknown, f"unknown={sorted(unknown)}")

    # ---------------------------------------------------------------- Layer 3
    print("\n== Layer 3: faithfulness to S3 source (sampled) ==")
    # Sample in SQL, not in Python. This used to fetch EVERY matching row and
    # then shuffle and keep `sample` of them -- invisible at 70 trials, but it
    # hauled all 117,877 gemini-code-rl-export rows over the wire to look at 50,
    # and harbor-forge (680k) or a full-scope run (~1.07M) would be far worse.
    # The scan still happens (harbor_config->>'...' is unindexed JSONB), but the
    # transfer and memory cost drop to the sample size.
    rows = list(await conn.fetch(
        f"SELECT id, reward, agent, model, orig_s3_src AS prefix "
        f"FROM trials WHERE {where} AND orig_s3_src IS NOT NULL "
        f"ORDER BY random() LIMIT {max(0, int(sample))}", *args))
    reward_mismatch = missing_prefix = missing_result = model_suspect = 0
    missing_log = 0
    session = aioboto3.Session()
    async with session.client("s3", region_name=settings.s3_region or "us-east-1") as s3:
        for r in rows:
            prefix = r["prefix"]
            # Compare trial.reward against the SAME sources build_spec reads, in
            # the same order: plain-number files first, then a reward.json
            # fallback. Checking only reward.txt would let a trial whose score
            # lives in reward-float.txt/reward.json pass unverified. A trial with
            # no reward source at all correctly has reward=None (not a mismatch).
            src = None
            for key in (f"{prefix}verifier/reward.txt",
                        f"{prefix}verifier/verifier/reward.txt",
                        f"{prefix}verifier/reward-float.txt"):
                try:
                    body = await (await s3.get_object(Bucket=DEFAULT_BUCKET, Key=key))["Body"].read()
                    src = float(body.decode().strip()); break
                except Exception:
                    continue
            if src is None:
                try:
                    body = await (await s3.get_object(
                        Bucket=DEFAULT_BUCKET, Key=f"{prefix}verifier/reward.json"))["Body"].read()
                    j = json.loads(body)
                    if isinstance(j, (int, float)):
                        src = float(j)
                    elif isinstance(j, dict):
                        for kk in ("reward", "score", "value"):
                            v = j.get(kk)
                            if isinstance(v, (int, float)):
                                src = float(v); break
                except Exception:
                    pass
            if src is not None:
                if r["reward"] is None or abs(float(r["reward"]) - src) > 1e-9:
                    reward_mismatch += 1
                    print(f"    reward mismatch {r['id']}: db={r['reward']} s3={src}")
            # Prove orig_s3_src points at a real trial dir WITHOUT betting on any
            # single filename. Two previous versions of this gate were wrong for
            # exactly that reason: result.json is missing for ~2% of genuine
            # trials (harness errors), and trial.log -- its replacement -- turned
            # out not to be universal either. It is present in attempt-layout
            # repos (reflection-ai, vals-experiments) but missing for some
            # flat-layout trials, which failed nov-5-export 6/50 even though
            # result.json AND the reward files were present at those same
            # prefixes, i.e. the prefixes were provably correct.
            #
            # A prefix listing cannot false-positive: if ANY object exists under
            # the prefix then the path is real, whatever the layout wrote there.
            try:
                listing = await s3.list_objects_v2(
                    Bucket=DEFAULT_BUCKET, Prefix=prefix, MaxKeys=1)
                if not listing.get("KeyCount"):
                    missing_prefix += 1
                    print(f"    empty prefix {r['id']}: {prefix}")
            except Exception as exc:
                missing_prefix += 1
                print(f"    prefix probe failed {r['id']}: {exc!r}")
            # Per-file presence is now INFO only -- useful for spotting a repo
            # whose layout differs, never a reason to fail the run.
            try:
                await s3.head_object(Bucket=DEFAULT_BUCKET, Key=f"{prefix}trial.log")
            except Exception:
                missing_log += 1
            try:
                await s3.head_object(Bucket=DEFAULT_BUCKET, Key=f"{prefix}result.json")
            except Exception:
                missing_result += 1
            # model un-collapse sanity: real agents shouldn't keep the lossy doubled prefix
            m = (r["model"] or "")
            if r["agent"] not in ("nop", "oracle") and any(
                m.startswith(p) for p in ("gemini-gemini-", "openai-openai-", "anthropic-anthropic-")):
                model_suspect += 1
    check("sampled rewards match reward.txt", reward_mismatch == 0,
          f"mismatches={reward_mismatch}/{len(rows)}")
    check("sampled orig_s3_src prefixes valid (objects exist under prefix)",
          missing_prefix == 0, f"empty={missing_prefix}/{len(rows)}")
    info("sampled trial.log present (info: not universal across layouts)",
         f"missing={missing_log}/{len(rows)}")
    info("sampled result.json present (info: some source trials lack it)",
         f"missing={missing_result}/{len(rows)}")
    check("model looks un-collapsed (no doubled prefix)", model_suspect == 0,
          f"suspect={model_suspect}/{len(rows)}")

    # ---------------------------------------------------------------- Layer 4
    print("\n== Layer 4: API smoke test (same endpoint accepts imported & native) ==")
    from oddish.core.endpoints.tasks_query import list_tasks_core
    from oddish.db import get_session

    imp_exp = await conn.fetchval(f"SELECT experiment_id FROM trials WHERE {where} LIMIT 1", *args)
    nat_exp = await conn.fetchval(
        "SELECT experiment_id FROM trials WHERE origin='oddish' AND org_id=$1 "
        "AND experiment_id IS NOT NULL LIMIT 1", org_id)
    for label, exp in [("imported", imp_exp), ("native", nat_exp)]:
        if exp is None:
            info(f"{label} experiment endpoint", "skipped (no experiment found)")
            continue
        try:
            async with get_session() as s:
                resp = await list_tasks_core(
                    s, experiment_id=exp, org_id=org_id, include_trials=True,
                    compact_trials=True, limit=200)
            ok = isinstance(resp, list) and len(resp) > 0
            check(f"{label} experiment renders (list_tasks_core 200)", ok,
                  f"exp={exp} tasks={len(resp) if resp else 0}")
        except Exception as e:  # a raise here == the UI would 500
            check(f"{label} experiment renders (list_tasks_core 200)", False, f"exc={e!r}")

    # ---------------------------------------------------------------- Layer 5
    print("\n== Layer 5: no re-execution (the ONE hard rule) ==")
    # The only forbidden side effect is a trial EXECUTION job (kind='TRIAL')
    # for an imported trial. Analysis/QA/verdict jobs are ALLOWED and expected
    # when imports merge into run_analysis=true tasks (stock importer behavior).
    # worker_jobs references its subject via (subject_table, subject_id).
    exec_jobs = await conn.fetchval(
        f"""SELECT count(*) FROM worker_jobs w
            WHERE w.kind = 'TRIAL'
              AND w.subject_table = 'trials'
              AND w.subject_id IN (SELECT id FROM trials WHERE {where})""",
        *args)
    check("no EXECUTION jobs for imported trials", exec_jobs == 0, f"exec_jobs={exec_jobs}")
    qa_jobs = await conn.fetchval(
        f"""SELECT count(*) FROM worker_jobs w
            WHERE w.kind <> 'TRIAL'
              AND ((w.subject_table = 'trials' AND w.subject_id IN
                      (SELECT id FROM trials WHERE {where}))
                OR (w.subject_table = 'tasks' AND w.subject_id IN
                      (SELECT id FROM tasks WHERE imported_at IS NOT NULL)))""",
        *args)
    info("analysis/QA/verdict jobs touching imports (allowed)", f"count={qa_jobs}")

    await conn.close()

    print("\n" + "=" * 60)
    if failures:
        print(f"RESULT: FAILED ({len(failures)}): {', '.join(failures)}")
    else:
        print("RESULT: ALL CHECKS PASSED")
    return not failures


@app.local_entrypoint()
def main(scope_base: str | None = None, scope_run: str | None = None,
         scope_pr: int | None = None, sample: int = 50) -> None:
    ok = validate.remote(scope_base=scope_base, scope_run=scope_run,
                         scope_pr=scope_pr, sample=sample)
    if not ok:
        raise SystemExit(1)
