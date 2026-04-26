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

- `oddish run` - submit a job
- `oddish upload` - register a task or upload existing trials
- `oddish status` - view progress
- `oddish cancel` - stop in-flight trials for a task
- `oddish pull` - download logs and artifacts
- `oddish delete` - delete task data

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

<details>
<summary>Options</summary>

- `--path`, `-p PATH` - Harbor-compatible path flag for a local task or dataset directory
- `--dataset`, `-d TEXT` - Registry dataset such as `swebench@1.0`
- `--task TEXT` - Append trials to an existing task ID instead of uploading task files
- `--config`, `-c PATH` - YAML or JSON config for multi-agent sweeps
- `--agent`, `-a TEXT` - Agent name for simple single-agent runs (defaults to `claude-code`)
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
- `--quiet`, `-q` - Suppress startup logs
- `--run-analysis` - Run trial analysis and compute a task verdict
- `--disable-verification` - Skip task verification or tests
- `--override-cpus INTEGER` - Override environment CPU count
- `--override-memory-mb INTEGER` - Override environment memory
- `--override-gpus INTEGER` - Override environment GPU count
- `--override-storage-mb INTEGER` - Override environment storage
- `--force-build/--no-force-build` - Force a rebuild of the environment image
- `--ae`, `--agent-env TEXT` - Pass agent env vars as `KEY=VALUE`; can be used multiple times
- `--ak`, `--agent-kwarg TEXT` - Pass agent kwargs as `key=value`; can be used multiple times
- `--artifact TEXT` - Download an environment path as an artifact after the trial
- `--api TEXT` - Override the API URL
- `--json` - Emit JSON for scripts and CI; implies `--background`

</details>

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
```

## Reading data from Oddish

Two commands cover all read access from the CLI:

- `oddish status` - inspect what is in flight and look up live state for a task or experiment.
- `oddish pull` - download logs, results, trajectories, and artifact files for a trial, task, or experiment.

If you have...                          | Use
----------------------------------------|----------------------------------------------
Nothing yet, want recent activity       | `oddish status`
A task ID *or* an experiment ID         | `oddish status <id>` (auto-detects either)
A specific trial ID and want its files  | `oddish pull <trial_id>`
A task ID and want every trial's output | `oddish pull <task_id>`
An experiment and want everything       | `oddish pull <experiment_id> --include-task-files`

The CLI does not currently support listing, filtering, or searching trials/tasks/experiments by status, name, or date. You generally discover IDs through the dashboard or `oddish status`, and then act on them with the commands below.

## Check Progress

Use `oddish status` to inspect the system, a task, or an experiment.

```bash
# System overview (most recent 8 experiments)
oddish status

# Task status — trials, rewards, stages, attempts
oddish status <task_id>

# Experiment status — all tasks in the experiment
oddish status --experiment <experiment_id> --watch
```

If a positional ID isn't found as a task, `status` automatically retries it as an experiment ID, so you can mostly forget which kind of ID you're holding.

`--watch` polls until the task or experiment reaches a terminal state (`completed` or `failed`).

The bare `oddish status` view groups the 200 most recent tasks into experiments and shows the eight most recently active. Older experiments are not paginated — pass `--experiment <id>` to inspect a specific one directly.

<details>
<summary>Options</summary>

- `TASK_ID` - Task ID to inspect when not using `--experiment`. If the ID is not a known task, it is retried as an experiment ID.
- `--experiment`, `-e TEXT` - Inspect an experiment instead of a task
- `--watch`, `-w` - Poll until the task or experiment reaches a terminal state
- `--verbose`, `-v` - Reserved; pipeline statistics are no longer reported by the CLI
- `--api TEXT` - Override the API URL

</details>

## Cancel In-Flight Runs

Use `oddish cancel` to stop queued or running work for a task without deleting
the task itself. Completed trials are preserved.

```bash
# Cancel all active runs for a task
oddish cancel <task_id>
```

<details>
<summary>Options</summary>

- `TASK_ID` - Task ID to cancel
- `--force`, `-f` - Skip the confirmation prompt
- `--api TEXT` - Override the API URL

</details>

## Download Outputs

Use `oddish pull` to download trial logs, structured logs, results, trajectories, and artifact files from Oddish to local disk. The same command works for a single trial, every trial in a task, or every trial in an experiment — the target type is auto-detected.

```bash
# Pull a single trial's logs, result, trajectory, and artifacts
oddish pull <trial_id>

# Pull every trial in a task, plus the task's own files
oddish pull <task_id> --include-task-files

# Pull every trial across an experiment into a custom directory
oddish pull <experiment_id> --include-task-files --out ./downloads

# Watch a running task and incrementally pull new artifacts as they appear
oddish pull <task_id> --watch --interval 10
```

By default, files are written to `./.oddish/<target>` with this layout:

- `manifest.json` - source, target, watch iteration, and a per-trial summary
- `trials/<trial_id>/logs.txt` - trial execution logs (when `--logs`, on by default)
- `trials/<trial_id>/logs_structured.json` - structured logs (when `--structured`)
- `trials/<trial_id>/result.json` - trial result
- `trials/<trial_id>/trajectory.json` - trial trajectory
- `trials/<trial_id>/<harbor paths...>` - trial artifact files (when `--files`, on by default), preserving Harbor's relative layout
- `tasks/<task_id>/task.json` - task metadata (for task and experiment targets)
- `tasks/<task_id>/files/<paths...>` - task-level files (when `--include-task-files` *and* `--files`)

Trial IDs follow the convention `<task_id>-<index>` (e.g. `t_abc123-0`). `oddish pull` uses this to auto-detect that a target is a trial; pass `--type` to skip detection if needed.

Re-pulling is idempotent: files already on disk whose size matches the remote are skipped. This makes `--watch` safe to leave running — each iteration only downloads new or changed artifacts, and watch stops automatically when the target reaches a terminal state.

Published (shared) experiments are reachable via the public read endpoints, so a pull will fall back to the public path automatically when private access isn't available.

<details>
<summary>Options</summary>

- `TARGET` - Trial ID, task ID, or experiment ID
- `--type [trial|task|experiment]` - Force target type instead of auto-resolving
- `--out`, `-o PATH` - Output directory (default `./.oddish/<target>`)
- `--logs/--no-logs` - Include trial logs (default: on)
- `--files/--no-files` - Include trial or task artifacts (default: on)
- `--structured` - Also save structured trial logs alongside `logs.txt`
- `--include-task-files` - Include task-level files for task or experiment targets (also requires `--files`)
- `--watch`, `-w` - Keep pulling while the run is in progress; stops on terminal state
- `--interval INTEGER` - Polling interval in seconds for `--watch` (default 5, minimum 1)
- `--api TEXT` - Override the API URL

</details>

### From a fresh experiment to local artifacts

```bash
# 1. See what's running
oddish status

# 2. Drill into one experiment and watch until it finishes
oddish status --experiment exp_abc --watch

# 3. Pick a trial of interest from the table (e.g. t_xyz-3) and pull just that one
oddish pull t_xyz-3

# 4. Or pull everything for the experiment in one shot
oddish pull exp_abc --include-task-files --out ./runs/exp_abc
```

## Delete Data

Use `oddish delete` to delete task data.

```bash
# Delete task
oddish delete <task_id>

# Delete an experiment
oddish delete --experiment <experiment_id>
```

<details>
<summary>Options</summary>

- `TASK_ID` - Task ID to delete when not using `--experiment`
- `--experiment`, `-e TEXT` - Delete an experiment instead of a task
- `--api-url`, `-u TEXT` - Override the API URL

</details>
