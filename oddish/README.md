# Oddish CLI

> Run Harbor tasks on Oddish infrastructure.

`oddish` is a Python CLI for submitting Harbor tasks, running multi-trial sweeps,
monitoring experiments, and pulling logs and artifacts back to disk. If you
already use `harbor run`, Oddish adds persistent state, retries, queueing, and
better operational tooling around the same task format.

Python `3.13` is required.

## Quick Start

```bash
uv pip install oddish

export ODDISH_API_KEY="ok_..."

# Submit a run
oddish run -d swebench@1.0 -a codex -m openai/gpt-5.2 --n-trials 3

# List and watch progress
oddish ls
oddish status
oddish status <task_id> --watch

# Pull logs and artifacts locally
oddish pull <task_id> --watch
```

The CLI targets Oddish Cloud by default. All API-backed commands require
`ODDISH_API_KEY`. For self-deployed instances, also set `ODDISH_API_URL`.

## Installation

```bash
uv pip install oddish
```

Common environment variables:

```bash
export ODDISH_API_KEY="ok_..."

# Point at a self-deployed instance instead of Oddish Cloud
# export ODDISH_API_URL="https://<workspace>--api.modal.run"

# Optional dashboard override
# export ODDISH_DASHBOARD_URL="https://www.oddish.app"
```

Need to deploy your own stack? See [`../SELF_HOSTING.md`](../SELF_HOSTING.md).
Need package internals, architecture, or development notes? See [`AGENTS.md`](../AGENTS.md).

## Commands

The installed console script is:

```bash
oddish --help
```

Available commands:

- `oddish run` uploads a local task or dataset, downloads a registry dataset, or expands a sweep config into trials
- `oddish upload` registers task bundles (no trials) or uploads off-oddish Harbor trial results (logs, rewards, tokens) onto an existing task
- `oddish ls` lists uploaded tasks with version, trial, reward, and experiment summaries
- `oddish status` shows system, task, or experiment status
- `oddish cancel` stops all in-flight runs for a task
- `oddish pull` downloads logs, results, trajectories, and artifact files for a trial, task, or experiment
- `oddish delete` deletes a task or experiment from a self-hosted deployment

### `oddish run`

Use `oddish run` for:

- a single local Harbor task directory
- a local dataset directory containing multiple tasks
- a Harbor registry dataset via `--dataset`
- a YAML or JSON sweep config via `--config`
- appending trials to an existing task via `--task`

Examples:

```bash
# Local task
oddish run ./my-task -a claude-code -m anthropic/claude-sonnet-4-5

# Local dataset
oddish run ./my-dataset -a codex -m openai/gpt-5.2 --n-trials 3

# Harbor registry dataset
oddish run -d swebench@1.0 -a codex -m openai/gpt-5.2 --n-trials 3

# Filter a dataset
oddish run -d swebench@1.0 -t "django__*" -l 10 -a claude-code

# Append new trials to an existing task
oddish run --task task_123 -a gemini-cli -m google/gemini-3.1-pro-preview --n-trials 3

# Submit in the background
oddish run ./my-task -a claude-code --background

# Script-friendly JSON output (implies --background)
oddish run ./my-task -a claude-code --json
```

Common flags:

- `PATH` or `-p, --path` selects a local task or dataset directory
- `-a, --agent` selects the agent
- `-m, --model` selects the model
- `--n-trials` runs multiple trials per task
- `-d, --dataset` pulls tasks from the Harbor registry
- `--task` appends trials to an existing task ID without re-uploading task files
- `-c, --config` loads a YAML or JSON sweep config
- `-t, --task-name`, `-x, --exclude-task-name`, and `-l, --n-tasks` filter datasets
- `-e, --env` selects the execution environment
- `-P, --priority`, `-E, --experiment`, `-u, --user`, `-G, --github-user`, and `--github-meta` attach scheduling and attribution metadata
- `-w, --watch / --no-watch` watches single-task submissions until completion
- `--background` submits and returns immediately
- `--json` emits machine-readable output and implies `--background`
- `-q, --quiet` suppresses nonessential output
- `--run-analysis` runs post-trial analysis and task verdict generation
- `--publish` publishes the experiment for public read-only access
- `--disable-verification` skips task verification
- `--override-cpus`, `--override-memory-mb`, `--override-gpus`, `--override-storage-mb`, and `--force-build` override environment settings
- `--ae`/`--agent-env`, `--ak`/`--agent-kwarg`, and `--artifact` pass Harbor agent/env configuration through to every submitted config
- `--api` overrides the API URL for a single invocation

Supported `--env` values:

- `docker`
- `daytona`
- `e2b`
- `modal`
- `runloop`
- `gke`

When `--env` is omitted:

- hosted Oddish (`*.modal.run`) defaults to `modal`
- other API URLs default to `docker`
- `--task` preserves the existing task's environment unless you override it

### Sweep Configs

`oddish run -c sweep.yaml` accepts YAML or JSON. A minimal config:

```yaml
agents:
  - name: claude-code
    model_name: anthropic/claude-sonnet-4-5
    n_trials: 3
  - name: codex
    model_name: openai/gpt-5.2
    n_trials: 3

dataset: swebench@1.0
n_tasks: 10
priority: low
```

You can also set `path`, `exclude_task_names`, and `experiment_id` in the
config file. Per-agent overrides use `env` and `kwargs`. Timeouts and
per-provider concurrency are no longer configured in sweep files; declare task
timeouts in `task.toml` and API concurrency at server startup.

### `oddish upload`

`oddish upload` covers two related flows. The mode is picked
automatically from the positional `PATH` you provide:

1. **Task upload** — if `PATH` is a Harbor task directory (or a
   dataset directory of tasks), the task bundle is uploaded to storage
   and a task row is created in the DB so it shows up in the task
   browser. No trials are queued.
2. **Trial import** — if `PATH` is a Harbor `job_dir` (or a parent
   `jobs_dir` with multiple job subdirs), each trial in the job is
   registered against an existing task as if it had run on Oddish.
   Imported trials show up in the experiment view with their reward,
   tokens, cost, phase timing, and artifacts; the only difference is
   an `origin = "imported"` flag on the trial row.

Task uploads:

```bash
# Upload a single local task
oddish upload ./my-task

# Upload every task in a local dataset directory
oddish upload ./my-dataset

# Upload all tasks from a Harbor registry dataset
oddish upload -d swebench@1.0

# Filter which tasks to upload
oddish upload ./my-dataset -t "django__*" -l 10
oddish upload -d swebench@1.0 -x "*-slow"
```

Trial imports from a local `harbor run`:

```bash
# Import every trial in a single Harbor job dir into an existing task
oddish upload ./jobs/my-task.claude-code.abcd --task task_123

# Pin the imported trials to a named experiment (new or existing)
oddish upload ./jobs/my-task.claude-code.abcd --task task_123 \
    --experiment my-local-sweep

# Import multiple Harbor jobs at once from a parent jobs directory
oddish upload ./jobs --task task_123 --experiment my-local-sweep

# One-shot: upload the task and import its trials in a single command
oddish upload ./jobs/my-task.claude-code.abcd --path ./my-task

# Register metadata only (no logs/trajectory uploads)
oddish upload ./jobs/my-task.claude-code.abcd --task task_123 \
    --skip-artifacts
```

Common flags:

- `PATH` selects the source (task dir, dataset dir, Harbor job dir,
  or Harbor jobs parent dir). `-p, --path` is an alias that also
  doubles as a one-shot task upload in trial-import mode.
- `-d, --dataset` pulls tasks from the Harbor registry (task-upload mode)
- `-t, --task-name`, `-x, --exclude-task-name`, and `-l, --n-tasks`
  filter datasets (task-upload mode)
- `-M, --message` attaches a description to the uploaded task version
  (task-upload mode)
- `-u, --user` attributes the created task row to a user (defaults to your authenticated identity — Clerk-linked email for API keys / dashboard sessions)
- `-P, --priority` sets the task priority (`low` or `high`) (task-upload mode)
- `--task` pins imported trials to an existing task ID (trial-import mode)
- `-E, --experiment` pins imported trials to a new or existing
  experiment; omitted, each import creates a fresh experiment
  (trial-import mode)
- `--skip-artifacts` registers imported trial metadata only, without
  logs/trajectory (trial-import mode)
- `--api`, `--json`, `-q, --quiet` match the other commands

Notes:

- Task rows uploaded this way appear in the task browser in `pending`
  state until their first trials run (or are imported).
- `oddish run --task <task_id> ...` attaches fresh trials to a
  previously-uploaded task.
- The target task for a trial import must have been created *without*
  `run_analysis` enabled. Imports skip the worker queue and cannot
  feed the analysis pipeline.
- Experiments can be heterogeneous — one experiment can mix trials
  that ran on Oddish with trials that were imported.

### `oddish ls`

List uploaded tasks using the same latest-version task browser API as the
dashboard.

Examples:

```bash
oddish ls
oddish ls --query django
oddish ls --limit 50
oddish ls --json
```

### `oddish status`

Without arguments, `oddish status` shows recent experiments and API health. Use
a task ID or `--experiment` to inspect a specific run, and `--watch` to resume
live monitoring later.

Examples:

```bash
# System overview
oddish status

# Task snapshot
oddish status <task_id>

# Watch a task
oddish status <task_id> --watch

# Watch an experiment
oddish status --experiment <experiment_id> --watch
```

### `oddish cancel`

Cancel all in-flight runs for a task without deleting any data. Queued jobs are
removed, running trials are cancelled, and active Modal workers are terminated
when applicable. Completed trials and their results are preserved.

```bash
oddish cancel <task_id>
oddish cancel <task_id> --force   # skip confirmation
```

### `oddish pull`

`oddish pull` accepts a trial ID, task ID, or experiment ID and auto-detects
the target type by default.

Examples:

```bash
# Pull one trial
oddish pull <trial_id>

# Keep syncing a task while it runs
oddish pull <task_id> --watch --interval 5

# Pull an entire experiment, including task files
oddish pull <experiment_id> --include-task-files
```

By default, pull output is written to `./.oddish/<target>` and includes a
`manifest.json` describing the fetch. Use `--no-logs`, `--no-files`,
`--structured`, `--include-task-files`, `--out`, and `--type` to control what
gets downloaded and where it lands. `--type trial|task|experiment` forces the
target type instead of auto-resolving it.

### `oddish delete`

Examples:

```bash
# Delete a task and its trials
oddish delete <task_id>

# Delete an entire experiment
oddish delete --experiment <experiment_id>
```

## Typical Workflow

```bash
# 1. Submit a run
oddish run -d swebench@1.0 -a claude-code -m anthropic/claude-sonnet-4-5

# 2. Inspect or watch it later
oddish status <task_id> --watch

# 3. Pull outputs when you want them locally
oddish pull <task_id> --watch
```

## More Technical Docs

- Package internals and implementation notes: [`AGENTS.md`](../AGENTS.md)
- Self-hosting and deployment: [`../SELF_HOSTING.md`](../SELF_HOSTING.md)
