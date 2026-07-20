# Running the Sauron → Oddish migration

Operational runbook for the `legacy_*` scripts in this directory. Everything here
is **one-off and temporary** — these scripts and this document should be deleted
once the migration is finished and the `leg_*` ledger tables are dropped.

Read the "Gotchas" section before your first run. Most of it is non-obvious, and
several items look like bugs when they are actually correct behaviour.

---

## 1. What this migration does

Legacy **Sauron** trial data lives only as an S3 layout in
`abundant-github-workflows-bucket`:

```
abundant-ai/{repo}/pr-{N}/run-{id}/agent-{key}:{model}/{task}/{trial}/...
```

`legacy_discover.py` already walked that bucket and built the **ledger** —
`leg_trial_ledger`, one row per trial keyed by `s3_prefix`. **The ledger is the
worklist and the source of truth for progress.** Everything else reads from it.

`legacy_transfer.py` reuses the production importer (`initialize_trial_import`,
`origin=IMPORTED`) to write tasks, experiments and trials into Postgres. Imported
trials are **terminal** — they skip the queue and can never re-execute.

Everything lands in org **`8ebde5d0`** ("Abundant").

Artifacts are **not** copied. `orig_s3_src` records the Sauron path; blobs stay in
the legacy bucket. Serving them is a separate follow-up.

---

## 2. Current state

Don't trust a hardcoded list — ask the DB:

```bash
modal run backend/scripts/legacy_prod_check.py --scope-base abundant-ai/<repo>
```

As of the last session: the alembic migration is applied
(`alembic_version_oddish` = `legacy_imported_at_001`), and `abundant-ai/experiments`
pr-509 (70 trials), `abundant-ai/reflection-ai` (3,520) and
`abundant-ai/tbench-hammer` (28,994) have been transferred and validated.

Remaining work as measured before those runs: **~1.07M trials in 18,531
task-groups**, dominated by `harbor-forge` (680k), `experiments` (142k),
`gemini-code-rl-export` (118k) and `nov-5-export` (94k).

---

## 3. Setup

You need surprisingly little. **No database credentials, no AWS keys, no Supabase
access** — every secret lives in Modal and is only read inside the container.

1. Clone `oddish` and check out the branch holding these scripts.
2. Install the venv (`uv sync`) — you need the `modal` CLI.
3. Modal auth for the **`abundant-ai`** workspace, environment **`main`**.
   The scripts reference secrets `oddish-prod` (env `main`) and `sauron-legacy`.

The transfer image mounts **your local `oddish/` package** (`add_local_dir`), so
your checkout is what runs. Stay on the intended branch.

### Windows only

The Modal CLI crashes with `'charmap' codec can't encode character '✓'`
unless you set UTF-8 first. PowerShell has no inline `VAR=x cmd` form:

```powershell
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
```

Modal lives at `.\.venv\Scripts\modal.exe`. On macOS/Linux just use `modal`.

---

## 4. The scripts

| Script | Writes? | Purpose |
|---|---|---|
| `legacy_prod_check.py` | no | Preflight: columns, alembic heads, ledger state, rollback preview |
| `legacy_shard_planning.py` | no | Task-group size distribution, for choosing shard counts |
| `legacy_transfer.py` | **yes** | The migration itself |
| `legacy_validate.py` | no | 5-layer correctness gate; exits non-zero on failure |
| `legacy_prod_rollback.py` | **yes** | Removes one chunk and resets its ledger rows |
| `legacy_qa_incident_probe.py` | no | Checks whether the migration caused QA/queue side effects |
| `legacy_discover.py` | **yes** | Ledger builder. Already run. **Do not re-run.** |

Scope flags on transfer: `--scope-base` (repo), `--scope-run`, `--scope-pr`,
`--limit`, `--concurrency`, `--retry-failed`, `--shard` / `--num-shards`.
`--execute` is required to write; without it the transfer prints scope counts and
exits.

---

## 5. Standard run

Work one `s3_base` at a time.

**Preflight.** Confirm the columns exist and the chunk is untransferred:

```bash
modal run backend/scripts/legacy_prod_check.py --scope-base abundant-ai/<repo>
```

Want `ALL CHECKS PASSED`, `alembic_version_oddish = legacy_imported_at_001`, and
ledger rows in `discovered`.

**Transfer, sharded.** Each shard is an independent container. Launch them in
parallel — PowerShell 5.1 has no `ForEach-Object -Parallel`, so use
`Start-Process` with per-shard logs:

```powershell
$N = 16
foreach ($i in 0..($N-1)) {
  $a = @("run","backend\scripts\legacy_transfer.py",
         "--scope-base","abundant-ai/<repo>","--execute",
         "--concurrency","16","--num-shards","$N","--shard","$i")
  Start-Process -NoNewWindow -FilePath ".\.venv\Scripts\modal.exe" -ArgumentList $a `
    -RedirectStandardOutput "shard$i.log" -RedirectStandardError "shard$i.err"
}
```

bash equivalent:

```bash
N=16
for i in $(seq 0 $((N-1))); do
  modal run backend/scripts/legacy_transfer.py \
    --scope-base abundant-ai/<repo> --execute \
    --concurrency 16 --num-shards $N --shard $i > shard$i.log 2>&1 &
done; wait
```

**Sweep up failures**, then **validate**:

```bash
modal run backend/scripts/legacy_transfer.py --scope-base abundant-ai/<repo> --execute --retry-failed
modal run backend/scripts/legacy_validate.py --scope-base abundant-ai/<repo>
```

`RESULT: ALL CHECKS PASSED` and exit 0 means the chunk is done. The validator is
usable as a gate in a script.

---

## 6. Choosing a shard count

Two independent limits:

- **Modal's 6h per-function timeout.** Each shard carries `trials / N`.
- **Throughput per shard**, measured at **~4.85 trials/s** with `--concurrency 16`
  when 4 shards run together.

So a shard finishes in roughly `(trials / N) / 4.85` seconds. Keep that under 6h
(21,600s) with margin.

Measured scaling: one container ≈ 8/s; four containers ≈ 4.85/s **each**
(~19.4/s aggregate). That's 2.4x for 4x containers — sub-linear, and the
suspected shared bottleneck is the Supabase transaction pooler
(`pooler.supabase.com:6543`). Expect per-shard rate to sag further as N grows;
**re-measure rather than extrapolating.**

Rough guide:

| base | trials | suggested N |
|---|---|---|
| harbor-forge | 680k | 24–32 (measure first) |
| experiments | 142k | 16 |
| gemini-code-rl-export | 118k | 16 |
| nov-5-export | 94k | 12–16 |

If per-shard throughput collapses as you add shards, more containers won't help —
the next lever is a **direct connection on port 5432** instead of the pooler.

---

## 7. Gotchas

**`tasks+=0` is usually correct.** Tasks are get-or-create by `(org_id, name)`,
and the counter only increments on *create*. Legacy trials **merge into
same-name pre-existing native tasks**. All 8 pilot tasks and 75 of 172
tbench-hammer shard-0 tasks already existed. Nothing is wrong.

**Never shard by row — shard by `task_id`.** The importer holds a per-task lock
and the run-order index is per-task, so a task must live in exactly one shard.
The hash partition guarantees that. Changing this will cause lock contention and
interleaved ordering.

**Experiments legitimately span shards.** A run-root covers multiple tasks, so
several shards reach the same experiment id concurrently. Creation uses
`INSERT ... ON CONFLICT DO NOTHING` for that reason. A 4-shard dry run claiming
~4,900 experiments for a base with far fewer is expected — the upsert dedupes.

**Resume is clean; no babysitting needed.** The transfer only ever writes
`transferred` or `failed`, never `transferring`. If a shard is killed or times
out, in-flight rows remain `discovered` and the same command picks them up.
Trials imported but not ledger-marked are caught by the deterministic idempotency
key and land in `skipped`, not duplicated.

**`--retry-failed` is not automatic.** The normal worklist is `status='discovered'`
only. A transient error marks a row `failed`, and no plain re-run will ever retry
it. Always finish a base with a `--retry-failed` pass.

**Three alembic version tables exist.** `alembic_version_oddish` and
`alembic_version_backend` are real (see `oddish/alembic/env.py` and
`backend/alembic/env.py`). The bare **`alembic_version` table is stale** and holds
a revision present in no branch. Do not trust it.

**Validator Layer 5 has a blind spot.** It counts jobs whose *subject* is an
imported trial or task. Because legacy trials merge into native tasks, a QA job on
a merged task carries a **native** `subject_id` and is invisible to it. To check
the reverse direction, run `legacy_qa_incident_probe.py`.

**QA suppression is process-local.** `legacy_transfer.py` monkeypatches
`oddish.queue.maybe_start_qa_stage` inside its own process so imports enqueue no
QA work while still transitioning task status correctly. Deployed prod code is
untouched. It only protects work done *by the transfer* — it is not a global
switch.

**`HARBOR_SHA` must match the lockfiles.** The image installs harbor from the
abundant-ai fork at a pinned commit. `uv pip install` honours
`[tool.uv] override-dependencies` but **ignores `[tool.uv.sources]`**, so without
that explicit install harbor resolves from PyPI — same version number, different
source, and missing `harbor.environments.kube_ops`, which kills the job on
`import oddish.queue`. If `uv.lock` moves, update `HARBOR_SHA`.

**One operator per scope.** Concurrent runs on the same shard won't corrupt
anything, but they duplicate work and add pooler pressure.

**Trust the ledger, not the `DONE` lines.** Summing `trials+=` across shard logs
will sometimes come up short, and it does NOT mean rows were lost. Modal preempts
containers and restarts them with the same input; the restarted attempt prints a
fresh `scope:` line and its `DONE` reports only that attempt. On
gemini-code-rl-export the `DONE` lines summed to 112,554 against 117,877 in the
base — shard0 had been preempted after 5,323 trials and restarted. The ledger
showed all 117,877 `transferred` and the validator agreed. To check whether a
shard restarted:

```bash
grep -c "^scope:" gem_shard0.log      # more than 1 = it was preempted and resumed
```

Preemption is normal and needs no intervention. Expect several on a multi-hour
base. It is also the strongest evidence the resume design works: the shard picked
up from the ledger with zero duplicates and zero losses, unattended.

**Total in-flight is the thing that matters, not shard count.** Measured on prod:

| config | in-flight | result |
|---|---|---|
| 1 x 8   | 8  | 9.25/s |
| 1 x 16  | 16 | 8.0/s |
| 4 x 8   | 32 | best measured -- 23.5/s (nov-5), 17.6/s (experiments) |
| 8 x 4   | 32 | 12.8/s -- same in-flight, worse shape |
| 4 x 16  | 64 | 19.4/s AND killed 25% of shards |

Use **4 shards x --concurrency 8**. Above ~32 in-flight the pooler starts
returning `TimeoutError` and throughput drops -- more concurrency is actively
worse, not just riskier.

**Per-trial cost varies a lot by base.** Same config gave 23.5/s on
nov-5-export, 17.6/s on experiments, 9.6/s on gemini-code-rl-export. Estimate
from the first ten minutes of the base you are actually running, not from a
previous one.

**Do not restart to chase a decaying rate unless shards are imbalanced.** Rate
decay near the end has two different causes. If most shards show `DONE` and one
grinds on, that is genuine imbalance and a restart redistributes the remainder.
If all shards are still running and the rate is just falling, that is the
small-group tail -- LPT dispatches big groups first, so runs end on thousands of
tiny groups where per-group overhead dominates. A restart cannot make small
groups cheaper. Check with `grep -l "^DONE" *_shard*.log` before deciding; on
abundant-ai/experiments a restart in the second case finished *slower* than
leaving it alone.

---

## 8. Rollback

Same script for any chunk; scope is a flag, nothing is hardcoded.

```bash
# dry run first - always
modal run backend/scripts/legacy_prod_rollback.py --scope-base abundant-ai/<repo>
# then
modal run backend/scripts/legacy_prod_rollback.py --scope-base abundant-ai/<repo> --execute
```

It matches on **both** `imported_at IS NOT NULL` and
`harbor_config->>'source' = 'sauron-migration'`. Since the transfer never mutates
a pre-existing task or experiment, **native rows are invisible to it**. It deletes
a parent only when left childless, runs in a single transaction, refuses to run
unscoped, and resets ledger rows to `discovered` so the chunk is re-runnable.

In the dry-run output, the line to read is `NATIVE tasks merged into N (never
touched)`. That is your guard against the one genuinely bad outcome.

Proven end-to-end on the 70-trial pilot: rollback → preflight showed org-wide
`imported_at` back to 0 → re-transfer → re-validate all passed.

---

## 9. When something fails

| Symptom | Cause | Action |
|---|---|---|
| `ModuleNotFoundError: harbor.environments.kube_ops` | harbor resolved from PyPI | Check `HARBOR_SHA` against `uv.lock` |
| `'charmap' codec can't encode` | Windows console encoding | Set the two UTF-8 env vars |
| `DeadlockDetectedError` on DDL | Alembic DDL vs live traffic | See `legacy_prod_apply_migration.py` — one statement per transaction |
| Shard dies mid-run | Timeout / preemption | Re-run the same command; resume is clean |
| `errors=N` in the summary | Per-trial failures | Read `error` in the ledger, fix, then `--retry-failed` |
| Validator ledger-completeness fails | Rows not `transferred`/`skipped` | Output names the statuses; usually needs `--retry-failed` |
| Long silence after `scope:` line | Manifest prefetch | Now parallel and prints its own timing |

---

## 10. Finishing up

When every base validates clean:

1. Full-scope validate with no `--scope-*` flags.
2. Confirm the ledger has no rows left in `discovered` or `failed`.
3. Decide what happens to artifact serving — blobs are still in the Sauron bucket
   and `trial_s3_key` deliberately points at Oddish's own bucket, so legacy trials
   currently render as "no files".
4. Drop the `leg_*` ledger tables, delete these scripts and this document.
