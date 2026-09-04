# Task trajectory metrics (steps, agent runtime, tokens)

Per-task trajectory statistics for the 784-task catalogue in `tasks.tsv`
(task, category, the sheet's Opus 4.8 and grok vendor-latest-learnability
pass cells, the linked oddish experiment ids, and any alternate oddish task
name). `task_metrics.csv` is the snapshot taken 2026-09-04 UTC against the
production oddish API; `raw_trials.json` (every trial fetched, ~90 MB) is
written next to the script and reused as a cache but is not committed.

```bash
export ODDISH_API_KEY=ok_...           # READ scope is enough
python3 fetch_task_metrics.py          # -> raw_trials.json, task_metrics.csv
python3 summarize.py task_metrics.csv  # coverage, per-category medians, flags
```

`TASKS_FILE=other.tsv` points the fetch at another task list, `WORKERS`
sets request concurrency (default 8), and `RAW_CACHE=path` reuses a previous
`raw_trials.json` so only tasks missing from it are fetched again.

## How a task's trials are chosen

* Task lookup: `GET /tasks/browse?query=<name>`, exact match on the sheet
  name or an `alt_names` entry; if nothing matches, the name with its last
  `.xxx` suffix removed is tried (oddish stores `post-train-apps-qwen2.5-1`
  for `post-train-apps-qwen2.5-1.5b`). Several matching task ids are
  narrowed to the ones whose linked experiments intersect the sheet's.
* Trials: `kind == agent`, not a probe, not superseded, not the nop/oracle
  baselines; restricted to the sheet's linked experiments and to the latest
  task version seen there (versions pooled only when that is what reproduces
  the sheet's Opus denominator).
* A trial is measured when it produced a real trajectory: terminal status,
  `total_steps` present, non-zero token usage. Agent timeouts and runs that
  ended with a non-zero agent exit but still carry a trajectory are kept and
  counted in `n_timeouts` / `n_exit_errors`; sandbox failures and zero-token
  runs are dropped (`n_excluded`).
* Model preference: Opus 4.8 (its `global.anthropic.…` and `anthropic-hdo/…`
  spellings pooled) > any other Opus > the non-Opus model with the most
  measured trials on the task.

## Columns of `task_metrics.csv`

* `doc_category`: a coarse 4-bucket grouping for the overview doc —
  `Database & Datastore Bug Repair` (all seven DB systems),
  `Scientific & Numerical Computing` (Science + numerical-method reproduction),
  `Spreadsheet Model Reconstruction`, and `Software & Model Engineering`
  (model post-training + build/optimize/exploit tasks).
* `db_system`: for database tasks, the system under repair (`DuckDB`, `SQLite`,
  `RocksDB`, `TimescaleDB`, `Qdrant`, `etcd`, `FAISS`); blank otherwise.
* `model_used`, `model_tier`, `model_variants`: the model summarised.
* `n_trials`, `passes`: measured trials of that model and how many scored
  reward 1.0. `n_timeouts`, `n_exit_errors`, `n_excluded` break down the
  population as above.
* `steps_*`: ATIF trajectory steps (`trials.total_steps`).
* `runtime_min_*`: agent wall-clock in minutes from
  `phase_timing.agent_execution` (fallbacks: `trajectory_duration_seconds`,
  then `finished_at - started_at`). Environment setup and verifier time are
  excluded.
* `tokens_total_*` = `input_tokens + output_tokens`; `input_tokens` already
  includes cache reads, which are also broken out as `tokens_cache_median`.
* `n_clean`, `passes_clean`, `*_clean`: the same statistics over runs with no
  error string at all (no timeout, no non-zero agent exit). The DB-debug
  sheets used this population for their pass-rate denominators; the Research
  sheets counted timeouts and exit-1 runs, so `n_trials` matches them.
* `opus_table`, `learn_table`: the sheet's cells, echoed for comparison. A
  `sheet opus a/b vs fetched c/d` note flags a disagreement with `n_trials`;
  check `n_clean` before treating it as a real discrepancy.
* `task_ids`, `task_version`, `experiments_used`: the exact trial population.

## Companion files

* `task_metrics_avg_steps_ge75.csv` — the 267 tasks whose mean steps are >= 75
  (the long-horizon tail), same columns as `task_metrics.csv`.
* `CANDIDATE_SET.md` — a candidate-set overview built on that filtered set, with
  the 4 `doc_category` categories (and the seven-system database breakdown), per-category complexity and pass rates, and
  highlighted examples.

Snapshot facts: all 784 tasks resolved; 783 are measured on Opus 4.8 and one
(`dengue-ade-branch`) on Opus 5 because all six of its Opus 4.8 runs were
zero-token failures. Of the 668 tasks with an Opus cell in the sheet, 666
reconcile with either `n_trials` or `n_clean`; `perk4-shared-csm` (the sheet
counted ten zero-token runs as failures) and `ctf-text-sender-einherjar-01`
are the exceptions and carry notes.
