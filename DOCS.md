# Oddish CLI

> Harbor-compatible CLI for submitting evals, tracking progress, pulling artifacts, and cleaning up runs.

## Installation

```bash
uv pip install "oddish @ git+https://github.com/abundant-ai/oddish.git#subdirectory=oddish"
```

Ensure your API key is set:

```bash
export ODDISH_API_KEY="ok_..."
```

## Usage

**Commands:**

- `oddish run` - submit work, retry failed trials, or re-run task-level QA
- `oddish upload` - register a task or upload existing trials
- `oddish ls` - list uploaded tasks
- `oddish status` - view progress
- `oddish cancel` - stop in-flight task runs or task-level QA jobs
- `oddish backfill-analysis` - (re)run trial analysis for a trial, task, or experiment
- `oddish pull` - download logs and artifacts
- `oddish combine` - merge several experiments into a new one
- `oddish collect` - gather trials from tasks/trial IDs into a shareable read-only collection
- `oddish experiment create` - build a collection experiment from explicit trial IDs
- `oddish delete` - delete task data (trial delete works on hosted Oddish; task/experiment delete is self-host only)
- `oddish publish` / `oddish unpublish` - toggle public read-only sharing for an experiment
- `oddish probe` - internal probe-trial helpers (`oddish probe`, `oddish probe skill add`)

Every command accepts `--json` for machine-readable output (CI / scripts / agents).

### Lifecycle

A typical run flows through these commands:

1. `oddish run` (or `oddish upload`) — submit a task, dataset, or sweep and get back a task ID and experiment ID.
2. `oddish status` — discover what's in flight, then drill into a specific task or experiment to see trial-level progress and rewards.
3. `oddish pull` — once you have a trial, task, or experiment ID, download its logs, results, trajectories, and artifact files to disk.
4. `oddish run --retry` — re-queue failed trials or re-run task-level QA.
5. `oddish cancel` / `oddish delete` — stop in-flight work or remove data when you're done.
6. `oddish publish` — share an experiment publicly (read-only) and get a link.

`oddish pull` accepts a trial, task, or experiment ID and auto-detects which kind it is; `oddish status` takes a task ID (falling back to experiment lookup) or `--experiment`. Tasks can be filtered by name, tags, status, agent/model, created date, and many trial and aggregate attributes with `oddish ls` (see [List Tasks](#list-tasks)); other IDs are typically discovered through the dashboard or `oddish status`.

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
- `--harbor TEXT` - Override the Harbor source/ref for this run (`main`, a tag/SHA, `org/repo@ref`, or a git URL@ref); defaults to the locked fork commit (env: `ODDISH_HARBOR`)
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
- `--publish/--no-publish` - Publish the experiment for public read-only access (off by default)
- `--watch/--no-watch`, `-w` - Watch progress after submission; enabled by default
- `--background`, `--async`, `-b` - Submit and return immediately
- `--quiet`, `-q` - Suppress startup logs
- `--run-analysis` - Run task-level QA (classify every trial's trajectory and compute the task verdict)
- `--run-probe` - Auto-enqueue a probe trial for the task version (off by default)
- `--disable-verification/--enable-verification` - Skip task verification or tests
- `--force-new-version` - Allocate a new task version even when the content is unchanged
- `--submit-concurrency INTEGER` - Max parallel task uploads/submissions (default: adaptive)
- `--override-cpus INTEGER` - Override environment CPU count
- `--override-memory-mb INTEGER` - Override environment memory
- `--override-gpus INTEGER` - Override environment GPU count
- `--override-storage-mb INTEGER` - Override environment storage
- `--force-build/--no-force-build` - Force a rebuild of the environment image
- `--environment-kwarg`, `--harbor-environment-kwarg TEXT` - Pass Harbor environment kwargs as `KEY=VALUE`; can be used multiple times
- `--ae`, `--agent-env TEXT` - Pass agent env vars as `KEY=VALUE`; can be used multiple times
- `--ak`, `--agent-kwarg TEXT` - Pass agent kwargs as `key=value`; can be used multiple times
- `--artifact TEXT` - Download an environment path as an artifact after the trial
- `--registry-login TEXT` - Per-run container-registry login as `username=USER,token=TOKEN[,registry=docker.io]`; repeatable and honored by `--retry`.
  Wrap comma-bearing values like `--registry-login "username=USER,token='a,b'"`.
  Credentials authenticate sandbox image pulls, are encrypted across the queue, and are logged out on teardown.
  Docker Hub creds can also come from `ODDISH_DOCKERHUB_USERNAME` / `ODDISH_DOCKERHUB_TOKEN`.
  Prefer a Docker Hub access token over an account password.
- `--retry` - Re-run an existing target instead of submitting new work (see below)
- `--qa` - With `--retry`: re-run the task-level QA job (classify every trial + synthesize the verdict) instead of retrying trials
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

# Re-run the task-level QA job (classify every trial + synthesize the verdict)
oddish run <task_id> --retry --qa

# Machine-readable summary of what was queued
oddish run <experiment_id> --retry -y --json
```

- Default (`--retry` alone) re-queues failed trials. For task and experiment
  targets, only trials currently in a `failed` state are retried.
- `--qa` re-runs the single task-level QA job: it re-classifies every live trial
  and synthesizes a fresh task verdict. A trial-shaped id resolves to its parent
  task; experiment targets run QA for each task.
- `--qa` requires `--retry`.
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

## Upload Without Running

Use `oddish upload` to register a task (or dataset of tasks) without submitting
trials, or to import existing off-oddish Harbor trial results. Re-uploading
unchanged task content is idempotent (no new version).

```bash
# Register a task or dataset
oddish upload ./my-task
oddish upload -d swebench@1.0

# Import Harbor job results into an existing task
oddish upload ./jobs --task <task_id>

# Upload the task, then import trials against it
oddish upload ./jobs --path ./my-task
```

Options

- `PATH` - Task dir, dataset dir, a Harbor job dir (with `result.json`), or a parent dir of job dirs
- `--path`, `-p PATH` / `--dataset`, `-d TEXT` - Task or dataset to register
- `--task-name`, `-t` / `--exclude-task-name`, `-x` / `--n-tasks`, `-l` - Task filters for dataset uploads
- `--task TEXT` - Import mode: target task ID for the imported trials
- `--experiment`, `-E TEXT` - Import mode: experiment to attach trials to (auto-generated if omitted)
- `--skip-artifacts` - Import mode: import metadata without logs/trajectories
- `--priority`, `-P TEXT` - Task row priority (default `low`)
- `--message`, `-M TEXT` - Task version description
- `--user`, `-u TEXT` - Author override
- `--quiet`, `-q` / `--json` / `--api TEXT`

## List Tasks

Use `oddish ls` to browse uploaded tasks with their latest version, trial
counts, reward summary, tags, last run time, and linked experiments.

```bash
oddish ls
oddish ls --query django
oddish ls --tag benchmark --not-tag wip
oddish ls --status COMPLETED --agent cursor-cli
oddish ls --created-within 7d --avg-score-min 80 --sort avg_score_desc
oddish ls --compare-by agent --compare-a cursor-cli --compare-b gemini-cli --compare-metric reward
oddish ls --json
```

`oddish ls` mirrors the full Tasks-page filter set: every filter available in the
dashboard is exposed as a flag and forwarded to the same `/tasks/browse` endpoint.
Filters are ANDed together. Run `oddish ls --help` for the authoritative,
always-current list with per-flag descriptions and accepted values.

Core options

- `--query`, `-q TEXT` - Filter tasks by name
- `--limit`, `-n INTEGER` - Maximum number of tasks to show (default 25, max 100)
- `--offset INTEGER` - Number of tasks to skip
- `--json` - Emit the raw task browser JSON response
- `--api TEXT` - Override the API URL

Tags

- `--tag TEXT` - Require this tag (repeatable; AND semantics)
- `--tag-any TEXT` - Match any of these tags (repeatable; OR semantics)
- `--not-tag TEXT` - Exclude tasks carrying any of these tags (repeatable)

Task-column filters

- `--status TEXT` - Task status (repeatable)
- `--priority TEXT` - Task priority (repeatable)
- `--verdict-status TEXT` - Task verdict status (repeatable)
- `--experiment-id TEXT` - Experiment id (repeatable)
- `--has-link` / `--no-has-link` - Task has (or lacks) a source link
- `--run-analysis` / `--no-run-analysis` - Task runs analysis
- `--run-probe` / `--no-run-probe` - Task runs probe
- `--created-after`, `--created-before` - Absolute created-time bounds (ISO datetime)
- `--created-within TEXT` - Rolling window (`24h`, `7d`, or `30d`); resolved to `--created-after`

Trial-level filters (task has ≥1 matching trial)

- `--agent TEXT` - Trial agent (repeatable)
- `--model TEXT` - Trial model (repeatable)
- `--agent-model TEXT` - Agent+model pair as `agent:model` (repeatable)
- `--provider TEXT` - Trial provider (repeatable)
- `--environment TEXT` - Trial environment (repeatable)
- `--trial-status TEXT` - Trial status (repeatable)
- `--origin TEXT` - Trial origin (repeatable)
- `--analysis-classification TEXT` - Trial analysis classification (repeatable)
- `--harbor-sha TEXT`, `--harbor-stage TEXT` - Harbor SHA / stage (repeatable)
- `--has-error` / `--no-has-error` - Trial has an error
- `--has-trajectory` / `--no-has-trajectory` - Trial has a trajectory
- `--trial-is-probe` / `--no-trial-is-probe` - Match probe trials
- `--min-attempts`, `--min-tokens`, `--max-tokens`, `--min-steps`, `--max-steps INTEGER`
- `--reward-min`, `--reward-max FLOAT` - Per-trial reward bounds (0.0–1.0)

Task aggregate filters (computed on the fly over a task's trials)

- `--avg-score-min`, `--avg-score-max FLOAT` - Average score percent (0–100)
- `--pass-rate-min`, `--pass-rate-max FLOAT` - Pass rate percent (0–100)
- `--total-tokens-min`, `--total-tokens-max INTEGER`
- `--runtime-total-min`, `--runtime-total-max FLOAT` - Total run time (seconds)
- `--runtime-avg-min`, `--runtime-avg-max FLOAT` - Avg run time per trial (seconds)
- `--total-trials-min`, `--completed-trials-min`, `--failed-trials-min INTEGER`
- `--pass-count-min`, `--partial-count-min`, `--fail-count-min`, `--harness-count-min INTEGER`

Sort

- `--sort TEXT` - `avg_score_(asc|desc)`, `total_tokens_(asc|desc)`, `runtime_total_(asc|desc)`, `runtime_avg_(asc|desc)`

Agent/model comparison (keep tasks where A beats B on a metric)

- `--compare-by TEXT` - Compare subject: `agent` or `model`
- `--compare-a TEXT`, `--compare-b TEXT` - Subjects A and B
- `--compare-metric TEXT` - `reward | runtime | tokens | steps | pass_rate`
- `--compare-agg TEXT` - `best | avg | median`
- `--compare-margin FLOAT`, `--compare-margin-unit TEXT` - Margin and unit (`pct` or `abs`)

Top performer (best subject per task)

- `--top-by TEXT` - Subject: `agent` or `model`
- `--top-value TEXT` - Subject that must be the task's top performer
- `--top-metric TEXT` - `reward | runtime | tokens | steps | pass_rate`

Match-any (OR-groups)

- `--or-groups TEXT` - Raw JSON list of condition dicts (same keys as the flat params); a task matches if it satisfies any group, ANDed with the flat filters

> A `--compare` filter only applies when a distinct `--compare-a`/`--compare-b`
> pair is given (missing `--compare-by`/`--compare-metric`/`--compare-agg` default
> to `agent`/`reward`/`best`); `--top-*` applies only when `--top-value` is set.

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
- `--verbose`, `-v` - Extra detail in the system overview
- `--api TEXT` - Override the API URL
- `--json` - Emit a single JSON snapshot (no live watch)

## Cancel In-Flight Runs

Use `oddish cancel` to stop queued or running work without deleting the task
itself. Completed trials are preserved. By default it cancels all active task
runs; use `--qa` to cancel only the task-level QA job.

```bash
# Cancel all active runs for a task
oddish cancel <task_id>

# Cancel only the in-flight QA job (classification + verdict)
oddish cancel <task_id> --qa
oddish cancel <trial_id> --qa   # a trial id resolves to its parent task
```

Options

- `TASK_ID` - Task or trial ID to cancel; with `--qa`, a trial ID resolves to its parent task
- `--qa` - Cancel the task's in-flight QA job only (classification + verdict)
- `--force`, `-f` - Skip the confirmation prompt
- `--api TEXT` - Override the API URL
- `--json` - Emit the cancellation result as JSON (implies `--force`)

## Backfill Analysis

Use `oddish backfill-analysis` to (re)run trial analysis (LLM trajectory classification + task verdict) for an experiment, a task, or a single trial. Pass exactly one of `--experiment`, `--task`, or `--trial`. By default only trials with no successful analysis yet are filled, and trials already analyzed (including ones whose analysis previously failed) are reused — pass `--force` to redo failed or already-complete analyses. The task verdict is recomputed either way.

```bash
oddish backfill-analysis --task <task_id>
oddish backfill-analysis --trial <trial_id> --force
oddish backfill-analysis --experiment <experiment_id> --enable-analysis
```

Options

- `--experiment TEXT` - Re-analyze all trials in an experiment
- `--task TEXT` - Re-analyze all trials in a task
- `--trial TEXT` - Re-analyze a single trial
- `--force` - Re-run analysis even for trials already analyzed. With `--trial`, re-runs just that trial; with `--task` or `--experiment`, re-runs all their trials.
- `--enable-analysis` - Also set `run_analysis=true` on the affected tasks so future trials auto-analyze.
- `--json` - Emit machine-readable output.
- `--api TEXT` - Override the API URL

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

## Collect Trials into a Shared Collection

Use `oddish collect` to gather trials — from whole tasks and/or explicit trial
IDs — into a new read-only **collection experiment**, and (by default) publish
it with a public share link. Source tasks and trials are referenced, not
copied.

```bash
# Collect the current-version trials of two tasks and publish
oddish collect --task <task_a> --task <task_b> --name my-collection

# Mix tasks and individual trials; keep it private
oddish collect <trial_id> --task <task_id> --no-publish

# Machine-readable output (includes public_token / public_url when published)
oddish collect --task <task_id> --json
```

Options

- `TRIAL_ID...` - Optional trial IDs to include (combine freely with `--task`)
- `--task`, `-t TEXT` - Task ID or name whose current-version trials are linked (repeatable)
- `--name`, `-n TEXT` - Collection name (default `collection`)
- `--publish/--no-publish` - Create a public read-only share link (default: publish). Publishing requires a full-scope API key.
- `--json` - Print the raw JSON response
- `--api-url`, `-u TEXT` - Override the API URL

`oddish experiment create` is the lower-level sibling: it builds a collection
from explicit trial IDs only, never publishes, and requires `--name`:

```bash
oddish experiment create --name my-set <trial_id_1> <trial_id_2>
```

## Delete Data

Use `oddish delete` to delete tasks, experiments, or trials. Against hosted
Oddish (oddish.app), only trial deletion (`--trial`) is available; whole-task
and whole-experiment deletion require a self-hosted instance.

```bash
# Delete an experiment
oddish delete --experiment <experiment_id>

# Delete a task
oddish delete <task_id>

# Delete one or more trials and emit a JSON result
oddish delete --trial <trial_id> --json
```

Options

- `TASK_ID` - Task ID to delete when not using `--experiment` (self-host only)
- `--experiment`, `-e TEXT` - Delete an experiment instead of a task (self-host only)
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
