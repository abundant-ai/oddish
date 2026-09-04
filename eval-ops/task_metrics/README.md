# Task trajectory metrics (steps, agent runtime, tokens)

One-off pull of per-task trajectory statistics for the task catalogue in
`tasks.tsv` (task, category, the sheet's Opus 4.8 and grok
vendor-latest-learnability pass cells, and the linked oddish experiment ids).
Snapshot taken 2026-09-04 UTC against the production oddish API.

```bash
export ODDISH_API_KEY=ok_...           # READ scope is enough
python3 fetch_task_metrics.py          # -> raw_trials.json, task_metrics.csv
python3 summarize.py task_metrics.csv  # coverage + per-category medians + flags
```

`TASKS_FILE=other.tsv` points the fetch at a different task list; `WORKERS`
sets the request concurrency (default 8).

## What one row of `task_metrics.csv` means

* `model_used` / `model_tier` / `model_variants`: the model whose trials the
  row summarises. Opus 4.8 is preferred (its provider spellings
  `global.anthropic.claude-opus-4-8` and `anthropic-hdo/claude-opus-4-8` are
  pooled), then any other Opus, then the non-Opus model with the most valid
  trials on the task.
* `n_trials` / `passes`: valid trials of that model and how many scored
  reward 1.0. `opus_table` and `learn_table` echo the sheet's cells so the
  two can be compared; a `sheet opus a/b vs fetched c/d` note flags any
  disagreement.
* `steps_*`: ATIF trajectory steps (`trials.total_steps`).
* `runtime_min_*`: agent wall-clock in minutes, from the trial's
  `phase_timing.agent_execution` (falls back to
  `trajectory_duration_seconds`, then `finished_at - started_at`). Environment
  setup and verifier time are excluded.
* `tokens_total_*` = `input_tokens + output_tokens`; `input_tokens` already
  includes cache reads, which are also broken out as `tokens_cache_median`.
* `task_ids`, `task_version`, `experiments_used`: the exact trial population.

Trial population per task: `kind == agent`, not a probe, not superseded, not
the nop/oracle baselines, restricted to the sheet's linked experiments and to
the latest task version seen there (versions are pooled only when that is
what reproduces the sheet's Opus denominator). Harness errors (non-timeout
`error_message`, e.g. exit 137/143 or a missing reward file) are excluded the
same way the sheet's denominators exclude them; agent/verifier timeouts are
kept because the trajectory is real.
