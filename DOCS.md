# Oddish CLI

> Harbor-compatible CLI for submitting evals, tracking progress, pulling artifacts, and cleaning up runs.

## Installation

```bash
uv pip install oddish
```

Ensure your API key is set:

```bash
export ODDISH_API_KEY="ok_..."
```

## Usage

**Commands:**

- `oddish run` - submit a job (or re-run trials/analysis/verdict with `--retry`)
- `oddish upload` - register a task or upload existing trials
- `oddish ls` - list uploaded tasks
- `oddish status` - view progress
- `oddish cancel` - stop in-flight trials for a task
- `oddish pull` - download logs and artifacts
- `oddish combine` - merge several experiments into a new one
- `oddish delete` - delete task data
- `oddish publish` / `oddish unpublish` - toggle public read-only sharing for an experiment

Every command accepts `--json` for machine-readable output (CI / scripts / agents).

### Lifecycle

A typical run flows through these commands:

1. `oddish run` (or `oddish upload`) — submit a task, dataset, or sweep and get back a task ID and experiment ID.
2. `oddish status` — discover what's in flight, then drill into a specific task or experiment to see trial-level progress and rewards.
3. `oddish pull` — once you have a trial, task, or experiment ID, download its logs, results, trajectories, and artifact files to disk.
4. `oddish run --retry` — re-queue failed trials, or re-run analysis/verdict, for a trial, task, or experiment.
5. `oddish cancel` / `oddish delete` — stop in-flight work or remove data when you're done.
6. `oddish publish` — share an experiment publicly (read-only) and get a link.

Both read commands accept a trial, task, or experiment ID and auto-detect which kind it is. The CLI does not yet support listing or filtering trials/tasks/experiments by status, name, or date — IDs are typically discovered through the dashboard or `oddish status`.

## Submit a Job

Use `oddish run` to launch a task, dataset, or multi-agent sweep.

```bash
# Single task
oddish run ./my-task -a claude-code -m anthropic/claude-sonnet-4-5 --n-trials 5

# Append trials to an existing task
oddish run --task <task_id> -a gemini-cli -m google/gemini-3.1-pro-preview

# Complex sweep from config
oddish run ./my-task -c sweep.yaml
```

Options

- `--path`, `-p PATH` - Harbor-compatible path flag for a local task or dataset directory
- `--dataset`, `-d TEXT` - Registry dataset such as `swebench@1.0`
- `--task TEXT` - Append trials to an existing task ID instead of uploading task files
- `--config`, `-c PATH` - YAML or JSON config for multi-agent sweeps
- `--agent`, `-a TEXT` - Agent name for simple single-agent runs (defaults to `claude-code`)
- `--model`, `-m TEXT` - Model override for the selected agent
- `--n-trials INTEGER` - Number of trials per task
- `--max-trial-attempts INTEGER` - Override the maximum Oddish attempts per trial, including the initial run
- `--task-name`, `-t TEXT` - Include task glob filter; can be passed multiple times
- `--exclude-task-name`, `-x TEXT` - Exclude task glob filter; can be passed multiple times
- `--n-tasks`, `-l INTEGER` - Limit the number of selected tasks after filtering
- `--env`, `-e` - Execution environment: `docker`, `daytona`, `e2b`, `modal`, `runloop`, or `gke`
- `--priority`, `-P TEXT` - Queue priority, typically `low` or `high`
- `--experiment`, `-E TEXT` - Reuse or create an experiment ID/name
- `--user`, `-u TEXT` - Override the author attached to the run. Defaults to the authenticated identity (Clerk-linked email for API keys / dashboard sessions); set this only to attribute a run to someone other than yourself.
- `--github-user`, `-G TEXT` - GitHub user attribution for CI metadata. When omitted, the backend auto-fills this from the authenticated user's Clerk-linked GitHub username (if any) so CI-style attribution still works.
- `--github-meta TEXT` - JSON metadata blob to attach to the task
- `--link TEXT` - Associate URL with the task. 
- `--publish` - Publish the experiment for public read-only access
- `--watch/--no-watch`, `-w` - Watch progress after submission; enabled by default
- `--background`, `--async`, `-b` - Submit and return immediately
- `--quiet`, `-q` - Suppress startup logs
- `--run-analysis` - Run trial analysis and compute a task verdict
- `--disable-verification` - Skip task verification or tests
- `--override-cpus INTEGER` - Override environment CPU count
- `--override-memory-mb INTEGER` - Override environment memory
- `--override-gpus INTEGER` - Override environment GPU count
- `--override-storage-mb INTEGER` - Override environment storage
- `--force-build/--no-force-build` - Force a rebuild of the environment image
- `--environment-kwarg`, `--harbor-environment-kwarg TEXT` - Pass Harbor environment kwargs as `KEY=VALUE`; can be used multiple times
- `--ae`, `--agent-env TEXT` - Pass agent env vars as `KEY=VALUE`; can be used multiple times
- `--ak`, `--agent-kwarg TEXT` - Pass agent kwargs as `key=value`; can be used multiple times
- `--artifact TEXT` - Download an environment path as an artifact after the trial
- `--retry` - Re-run an existing target instead of submitting new work (see below)
- `--analysis` - With `--retry`: re-run analysis instead of trials
- `--verdict` - With `--retry`: re-run the task verdict instead of trials
- `--yes`, `-y` - Skip confirmation prompts (used with `--retry`)
- `--api TEXT` - Override the API URL
- `--json` - Emit JSON for scripts and CI; implies `--background`

### Re-run with `--retry`

`oddish run --retry` re-runs existing work instead of submitting new trials. It
accepts a trial, task, or experiment id — positional, `--task`, or
`--experiment` — and auto-detects the target type.

```bash
# Retry a single failed trial
oddish run <trial_id> --retry

# Retry every failed trial in a task (skip the confirmation prompt)
oddish run <task_id> --retry -y

# Retry all failed trials across an experiment
oddish run <experiment_id> --retry -y

# Re-run analysis or the task verdict instead of trials
oddish run <task_id> --retry --analysis
oddish run <task_id> --retry --verdict

# Machine-readable summary of what was queued
oddish run <experiment_id> --retry -y --json
```

- Default (`--retry` alone) re-queues failed trials. For task and experiment
targets, only trials currently in a `failed` state are retried.
- `--analysis` re-runs trial analysis (per-trial for a trial target, otherwise
task-wide); `--verdict` re-runs the task verdict.
- `--analysis` and `--verdict` are mutually exclusive and require `--retry`.
- `-y, --yes` skips the confirmation prompt; `--json` is always non-interactive.

### Sweep Config

Use `oddish run -c sweep.yaml` to run multiple agents:

```yaml
agents:
  - name: claude-code
    model_name: anthropic/claude-sonnet-4-5
    n_trials: 3
  - name: codex
    model_name: openai/gpt-5.3-codex
    n_trials: 3
  - name: nop
    n_trials: 3
  - name: oracle
    n_trials: 3

max_trial_attempts: 3
harbor:
  environment:
    kwargs:
      agent_tools_image: ghcr.io/org/harbor-agent-tools:tag
```

`max_trial_attempts` is optional. It is the total Oddish worker attempt budget
per trial, including the initial run. When omitted, Oddish keeps its default
retry behavior.

## List Tasks

Use `oddish ls` to browse uploaded tasks with their latest version, trial
counts, reward summary, last run time, and linked experiments.

```bash
oddish ls
oddish ls --query django
oddish ls --limit 50
oddish ls --json
```

Options

- `--query`, `-q TEXT` - Filter tasks by name
- `--limit`, `-n INTEGER` - Maximum number of tasks to show
- `--offset INTEGER` - Number of tasks to skip
- `--json` - Emit the raw task browser JSON response
- `--api TEXT` - Override the API URL

## Check Progress

Use `oddish status` to inspect the system, a task, or an experiment.
Task status tables include a `Detail` column for the current Harbor stage or
terminal reason, such as `cancelled by user`.

```bash
# System overview
oddish status

# Task status
oddish status <task_id>

# Experiment status
oddish status --experiment <experiment_id> --watch

# Single JSON snapshot (no live watch) for scripts/agents
oddish status <task_id> --json
```

If a positional ID isn't found as a task, `status` automatically retries it as an experiment ID.

Options

- `TASK_ID` - Task ID to inspect when not using `--experiment`; falls back to experiment lookup if no matching task exists
- `--experiment`, `-e TEXT` - Inspect an experiment instead of a task
- `--watch`, `-w` - Poll until the task or experiment finishes
- `--api TEXT` - Override the API URL
- `--json` - Emit a single JSON snapshot (no live watch)

## Cancel In-Flight Runs

Use `oddish cancel` to stop queued or running work for a task without deleting
the task itself. Completed trials are preserved.

```bash
# Cancel all active runs for a task
oddish cancel <task_id>
```

Options

- `TASK_ID` - Task ID to cancel
- `--force`, `-f` - Skip the confirmation prompt
- `--api TEXT` - Override the API URL
- `--json` - Emit the cancellation result as JSON (implies `--force`)

## Download Outputs

Use `oddish pull` to download logs and artifacts from Oddish to local files.

```bash
# Pull a single trial
oddish pull <trial_id>

# Pull an experiment into a custom directory
oddish pull <experiment_id> --include-task-files --out ./downloads
```

By default, files are written to `./.oddish/<target>`. Re-pulling is idempotent — files already on disk that match the remote size are skipped, so `--watch` only downloads new or changed artifacts on each iteration and stops when the target reaches a terminal state.

Options

- `TARGET` - Trial ID, task ID, or experiment ID
- `--type [trial|task|experiment]` - Force target type instead of auto-resolving
- `--out`, `-o PATH` - Output directory
- `--logs/--no-logs` - Include trial logs
- `--files/--no-files` - Include trial or task artifacts
- `--structured` - Save structured trial logs in addition to normal logs
- `--include-task-files` - Include task-level files for task or experiment targets
- `--watch`, `-w` - Keep pulling while the run is in progress
- `--interval INTEGER` - Poll interval in seconds for `--watch`
- `--api TEXT` - Override the API URL
- `--json` - Print the pull manifest as JSON instead of progress output

## Targeting a PR Preview

Every open PR gets its own isolated preview stack: a Modal app
(`oddish-pr-<N>`), a Supabase Postgres branch, and a Vercel preview build —
provisioned automatically by `.github/workflows/pr-preview.yml`. To point
the CLI at a preview from your laptop:

```bash
# 1. Point at the preview backend by PR number.
export ODDISH_PREVIEW_PR=35

# 2. Sign in at the preview Vercel URL (printed in the PR's
#    Actions step summary), create an API key in the dashboard,
#    and export it. Preview keys are formatted `ok_pr-<N>_<hex>`
#    so a stray paste into a prod context is visually obvious.
export ODDISH_API_KEY=ok_pr-35_…

# 3. Run as usual — every command now hits the preview Modal +
#    Supabase branch DB.
oddish run /path/to/task --agent gemini-cli --model google/gemini-3.1-pro-preview
oddish status
```

API URL resolution order is `ODDISH_API_URL` (explicit) >
`ODDISH_PREVIEW_PR` (derived) > prod default. Forks change the URL
pattern by setting `ODDISH_PREVIEW_URL_TEMPLATE` (with `{n}` for the
PR number).

## Combine Experiments

Use `oddish combine` to merge two or more experiments into a brand-new
result experiment. The source experiments are left untouched; their task
memberships and finished trials (with artifacts) are copied into the new
experiment, so you get a single rolled-up view.

```bash
# Combine two experiments (by ID or name)
oddish combine <experiment_a> <experiment_b>

# Name the result and combine three experiments
oddish combine <exp_a> <exp_b> <exp_c> --name nightly-rollup

# Reference source artifacts in place instead of duplicating them
oddish combine <exp_a> <exp_b> --no-copy-artifacts
```

In-flight trials (still pending/queued/running) have no result to combine
and are skipped; the response reports how many were copied vs. skipped.

Options

- `SOURCE_EXPERIMENT_IDS...` - Two or more experiment IDs or names to combine
- `--name`, `-n TEXT` - Name for the result experiment (auto-generated if omitted)
- `--copy-artifacts / --no-copy-artifacts` - Duplicate each copied trial's
artifacts so the result is fully independent (default), or reference the
source artifacts in place (cheaper, shared storage)
- `--json` - Print the raw JSON response
- `--api-url`, `-u TEXT` - Override the API URL

## Delete Data

Use `oddish delete` to delete task data.

```bash
# Delete task
oddish delete <task_id>

# Delete an experiment
oddish delete --experiment <experiment_id>

# Delete one or more trials and emit a JSON result
oddish delete --trial <trial_id> --json
```

Options

- `TASK_ID` - Task ID to delete when not using `--experiment`
- `--experiment`, `-e TEXT` - Delete an experiment instead of a task
- `--trial`, `-t TEXT` - Delete one or more trials (repeatable); works against hosted Oddish
- `--yes`, `-y` - Skip confirmation prompts
- `--api-url`, `-u TEXT` - Override the API URL
- `--json` - Emit the delete result as JSON (implies `--yes`)

## Share an Experiment

Use `oddish publish` to make an experiment publicly viewable (read-only) and
get a shareable URL; `oddish unpublish` revokes it. Public viewers never see
trial analysis or task verdicts. (Both require a hosted/cloud deployment.)

```bash
# Publish and print the public URL
oddish publish <experiment_id>

# Machine-readable output (public URL + token)
oddish publish <experiment_id> --json

# Stop sharing
oddish unpublish <experiment_id>
```

Options

- `EXPERIMENT_ID` - Experiment ID (or name) to publish/unpublish
- `--api TEXT` - Override the API URL
- `--json` - Emit the share status as JSON

## Drag-and-drop import (UI)

The dashboard's **Tasks** page has an **Import** button next to the
search input that opens the same flow as `oddish upload`, but driven
from the browser. Drop one or both of:

- a Harbor task zip (e.g. `zip -r my-task.zip my-task`)
- a Harbor run zip — either a single job dir (with `result.json`) or a
parent dir of job dirs

The dialog accepts:

- **Task only** → registers a new task version (or no-op when content
is unchanged).
- **Run only** → imports every Harbor trial in the zip into the target
task ID you provide.
- **Task + run** → uploads the task first, then imports the trials
against it (the UI equivalent of `oddish upload ./jobs --path ./my-task`).

The optional **Experiment name** field maps to `--experiment`; leaving
it blank auto-generates a fresh experiment, matching the CLI default.
**Skip artifacts** maps to `--skip-artifacts`. Re-uploading the same
task content is idempotent — content-hash unchanged → no new version.

For very large archives or scripted/CI flows, prefer the CLI: the UI
caps each uploaded zip at 1 GiB.