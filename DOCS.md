# Oddish CLI

`oddish` is a Harbor-compatible CLI for running evals with persisted state, queued execution, retries, and monitoring.

**Commands**

- `oddish run` — submit a job
- `oddish status`— view progress
- `oddish pull` — download logs and artifacts
- `oddish clean` — delete task or trial data

## Setup

```bash
uv pip install oddish
export ODDISH_API_KEY="ok_..."
```

## `oddish run`

```bash
# Single task
oddish run ./my-task -a claude-code -m anthropic/claude-sonnet-4-5 --n-trials 5

# Registry dataset
oddish run -d terminal-bench@2.0 -a claude-code -m anthropic/claude-sonnet-4-5 --n-trials 3

# Complex sweep from config
oddish run ./my-task -c sweep.yaml
```

<details>
<summary>options</summary>

- `--path`, `-p PATH` - Harbor-compatible path flag for a local task or dataset directory
- `--dataset`, `-d TEXT` - Registry dataset such as `swebench@1.0`
- `--config`, `-c PATH` - YAML or JSON config for multi-agent sweeps
- `--agent`, `-a TEXT` - Agent name for simple single-agent runs
- `--model`, `-m TEXT` - Model override for the selected agent
- `--n-trials INTEGER` - Number of trials per task
- `--task-name`, `-t TEXT` - Include task glob filter; can be passed multiple times
- `--exclude-task-name`, `-x TEXT` - Exclude task glob filter; can be passed multiple times
- `--n-tasks`, `-l INTEGER` - Limit the number of selected tasks after filtering
- `--env`, `-e` - Execution environment: `docker`, `daytona`, `e2b`, `modal`, `runloop`, or `gke`
- `--priority`, `-P TEXT` - Queue priority, typically `low` or `high`
- `--experiment`, `-E TEXT` - Reuse or create an experiment ID/name
- `--user`, `-u TEXT` - Override the user name attached to the run
- `--github-user`, `-G TEXT` - GitHub user attribution for CI metadata
- `--github-meta TEXT` - JSON metadata blob to attach to the task
- `--publish` - Publish the experiment for public read-only access
- `--watch/--no-watch`, `-w` - Watch progress after submission; enabled by default
- `--background`, `--async`, `-b` - Submit and return immediately
- `--quiet`, `-q` - Suppress local infrastructure startup logs
- `--run-analysis` - Run trial analysis and compute a task verdict
- `--disable-verification` - Skip task verification/tests
- `--override-cpus INTEGER` - Override environment CPU count
- `--override-memory-mb INTEGER` - Override environment memory
- `--override-gpus INTEGER` - Override environment GPU count
- `--override-storage-mb INTEGER` - Override environment storage
- `--force-build/--no-force-build` - Force a rebuild of the environment image
- `--ae`, `--agent-env TEXT` - Pass agent env vars as `KEY=VALUE`; can be used multiple times
- `--ak`, `--agent-kwarg TEXT` - Pass agent kwargs as `key=value`; can be used multiple times
- `--artifact TEXT` - Download an environment path as an artifact after the trial
- `--api TEXT` - Override the API URL
- `--fresh` - Restart the local API server before running
- `--json` - Emit JSON for scripts and CI; implies `--background`

</details>

## Sweep Config

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
```

## `oddish status`

```bash
# System overview
oddish status

# Task status
oddish status <task_id>

# Experiment status
oddish status --experiment <experiment_id> --watch
```

<details>
<summary>options</summary>

- `TASK_ID` - Task ID to inspect when not using `--experiment`
- `--experiment`, `-e TEXT` - Inspect an experiment instead of a task
- `--watch`, `-w` - Poll until the task or experiment finishes
- `--verbose`, `-v` - Request extra system output
- `--api TEXT` - Override the API URL

</details>

## `oddish pull`

Download logs and artifacts from Oddish to local files.

Examples:

```bash
# Pull a single trial
oddish pull <trial_id>

# Pull an experiment into a custom directory
oddish pull <experiment_id> --include-task-files --out ./downloads
```

By default, files are written to `./oddish-pulls/<target>`.

<details>
<summary>options</summary>

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

</details>

## `oddish clean`

Stop local Oddish infrastructure or delete task data.

Examples:

```bash
# Stop local services and delete local data
oddish clean

# Stop local services but keep data
oddish clean --stop-only

# Delete one task through the API
oddish clean <task_id>

# Delete an experiment through the API
oddish clean --experiment <experiment_id>
```

<details>
<summary>options</summary>

- `TASK_ID` - Task ID to delete when not using `--experiment`
- `--experiment`, `-e TEXT` - Delete an experiment instead of a task
- `--stop-only` - Stop local infrastructure without deleting data
- `--api-url`, `-u TEXT` - Override the API URL used for cleanup

</details>
