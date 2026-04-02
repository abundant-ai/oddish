# Oddish CLI

> Run Harbor tasks on Oddish infrastructure.

`oddish` is a Python CLI for submitting Harbor tasks, running multi-trial sweeps,
monitoring experiments, and pulling logs and artifacts back to disk. If you
already use `harbor run`, Oddish adds persistent state, retries, queueing, and
better operational tooling around the same task format.

Python `3.12+` is required.

## Quick Start

```bash
uv pip install oddish

export ODDISH_API_KEY="ok_..."

# Submit a run
oddish run -d swebench@1.0 -a codex -m openai/gpt-5.2 --n-trials 3

# Watch progress
oddish status

# Pull logs and artifacts locally
oddish pull <task_id> --watch
```

The CLI targets Oddish Cloud by default. Set `ODDISH_API_KEY` to authenticate.
For self-deployed instances, also set `ODDISH_API_URL`.

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
Need package internals, architecture, or development notes? See [`AGENTS.md`](AGENTS.md).

## Commands

The installed console script is:

```bash
oddish --help
```

Available commands:

- `oddish run` submits a task, dataset, or sweep config
- `oddish status` shows system, task, or experiment status
- `oddish cancel` stops all in-flight runs for a task
- `oddish pull` downloads logs and artifact files locally
- `oddish delete` deletes task data or resets local infrastructure

### `oddish run`

Use `oddish run` for:

- a single local Harbor task directory
- a local dataset directory containing multiple tasks
- a Harbor registry dataset via `--dataset`
- a YAML or JSON sweep config via `--config`

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
```

Common flags:

- `-a, --agent` selects the agent
- `-m, --model` selects the model
- `--n-trials` runs multiple trials per task
- `-d, --dataset` pulls tasks from the Harbor registry
- `--task` appends trials to an existing task ID without re-uploading task files
- `-c, --config` loads a YAML or JSON sweep config
- `-t, --task-name` and `-x, --exclude-task-name` filter tasks by glob
- `-l, --n-tasks` limits how many tasks run
- `-e, --env` selects the execution environment
- `--experiment` groups runs into an explicit experiment
- `-w, --watch / --no-watch` watches task progress until completion
- `--background` submits and returns immediately
- `-q, --quiet` suppresses output
- `--run-analysis` runs post-trial analysis and verdict generation
- `--publish` publishes experiment for public read-only access
- `--priority` sets priority (low or high)
- `--disable-verification` skips running task tests

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

Per-agent overrides such as environment variables, kwargs, and timeouts are
passed through Harbor agent config fields.

### `oddish status`

Examples:

```bash
# System overview
oddish status

# Watch a task
oddish status <task_id> --watch

# Watch an experiment
oddish status --experiment <experiment_id> --watch
```

### `oddish cancel`

Cancel all in-flight runs for a task without deleting any data. Queued jobs are
removed, running trials are marked as failed, and Modal worker containers are
terminated. Completed trials and their results are preserved.

```bash
oddish cancel <task_id>
oddish cancel <task_id> --force   # skip confirmation
```

### `oddish pull`

Examples:

```bash
# Pull one trial
oddish pull <trial_id>

# Keep syncing a task while it runs
oddish pull <task_id> --watch --interval 5

# Pull an entire experiment, including task files
oddish pull <experiment_id> --include-task-files
```

By default, pull output is written to `./.oddish/<target>`. You can also configure what to download using `--no-logs`, `--no-files`, and `--structured` (for structured logs), or change the output directory with `--out`.

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

# 2. Inspect or watch it
oddish status
oddish status <task_id> --watch

# 3. Pull outputs when you want them locally
oddish pull <task_id> --watch
```

## More Technical Docs

- Package internals and implementation notes: [`AGENTS.md`](AGENTS.md)
- Self-hosting and deployment: [`../SELF_HOSTING.md`](../SELF_HOSTING.md)
