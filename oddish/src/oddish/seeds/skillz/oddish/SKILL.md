---
name: oddish
description: "Use when working with the Oddish CLI or Oddish Cloud for Harbor task experiments: checking Oddish auth/status, listing or searching recent tasks with oddish ls, pulling full run logs/artifacts/analysis, interpreting pulled artifact directories, or debugging Oddish CLI/API behavior."
---

# Oddish

## Operating Rules

- Treat Oddish state as live and time-sensitive. Query the CLI/API before answering about current runs.
- Never print or persist `ODDISH_API_KEY`. If auth is missing, report that `ODDISH_API_KEY` is required.
- Prefer the installed CLI for normal operations, then use the REST API for filters the CLI does not expose.
- Use absolute output paths for pulls and summaries so the user can open the files directly.

## Quick Orientation

1. Check the CLI and auth:

```bash
command -v oddish
oddish --help
oddish status
```

2. Keep the overall CLI shape in mind:

```text
oddish run      # submit Harbor tasks/datasets/sweeps
oddish upload   # upload tasks or import off-Oddish Harbor results
oddish ls       # list uploaded tasks
oddish status   # inspect system/task/experiment status
oddish cancel   # cancel in-flight runs
oddish delete   # delete task/experiment
oddish pull     # download logs, trajectories, artifacts
```

3. If `oddish status` reports `Missing API token`, ask the user to provide/export `ODDISH_API_KEY`. The default cloud API is usually available from:

```bash
python -c "from oddish.cli.config import get_api_url; print(get_api_url())"
```

4. For task listing and name search, use `oddish ls` instead of scraping the `oddish status` table:

```bash
oddish ls
oddish ls --query django
oddish ls --limit 50
oddish ls --json
```

## Common Workflows

### Find Recent Tasks

Use `oddish ls` when the user asks for recent tasks, task name search, or a compact task table.

Useful flags:

- `--query <text>` / `-q <text>` filters tasks by name.
- `--limit <n>` / `-n <n>` controls page size. The CLI defaults to 25 and caps at 100.
- `--offset <n>` pages through additional results.
- `--json` emits the raw `/tasks/browse` response for scripting, `jq`, or CI.
- `--api <url>` overrides the API URL.

Table output shows task ID, name, latest version, trial counts, reward summary, last run time, and linked experiments. If table output says more results are available, rerun with the suggested `--offset`.

`oddish ls --query` searches task names. If the user needs filters that `oddish ls` does not expose, such as GitHub user attribution, use `oddish ls --json` first and fall back to direct API inspection only if the raw browser response does not contain enough data.

### Pull Full Run Artifacts

Use `oddish pull` for the actual artifact download:

```bash
oddish pull <experiment-id-or-task-id-or-trial-id> \
  --out /absolute/path/to/output \
  --structured \
  --include-task-files
```

Let `oddish pull` auto-resolve the target type by default. Add `--type experiment`, `--type task`, or `--type trial` only when auto-resolution is ambiguous or fails.

If a full pull times out on structured logs or large artifacts:

1. Re-run the same command. Existing files are skipped by size.
2. If structured logs are the failure point, re-run without `--structured`, then fetch missing structured logs manually or with a small API loop.
3. Check for `_pull_errors` files and retry those individual trial files before declaring the bundle complete.
4. Poll remote status before finalizing. Running trials do not have final result/analysis artifacts yet.

For long-running experiments, do an initial pull of completed trials, wait or poll, then run a final incremental pull.

### Summarize A Pull

After a pull, generate a compact summary:

```bash
ODDISH_SKILL_DIR=/absolute/path/to/skills/oddish
python "$ODDISH_SKILL_DIR/scripts/summarize_pull.py" /absolute/path/to/pull-root
```

This writes `analysis-summary.json` and `analysis-summary.md` next to the pull manifest. Use these as first-pass entry points for the user.

## Artifact Interpretation

Read `references/artifact-layout.md` when explaining what Oddish pulls contain or when mapping local files to logs, analysis, trajectories, results, verifier output, and task source.

Short version:

- `tasks/<task-id>/task.json` is the main metadata and analysis payload.
- `trials/<trial-id>/logs.txt` is raw run logging.
- `trials/<trial-id>/logs_structured.json` is parsed logging, not LLM analysis.
- `trials/<trial-id>/result.json` is the terminal result payload.
- `trials/<trial-id>/trajectory.json` is the agent trajectory when available.
- `trials/<trial-id>/task-.../` is the downloaded Harbor trial workspace/artifacts.
- `tasks/<task-id>/files/` is the original task source.

## API Notes

The current task browser endpoint used by `oddish ls` is:

```text
GET /tasks/browse?limit=<n>&offset=<n>&query=<text>
```

The API requires `Authorization: Bearer $ODDISH_API_KEY`. Prefer `oddish ls --json` over hand-written API calls when the CLI output shape is sufficient.

## Installation/Packaging Checks

If the `oddish` command exists but import/CLI modules are missing, inspect the Python package installation before debugging cloud state:

```bash
python - <<'PY'
import importlib.metadata as m, oddish
print(m.version("oddish"))
print(oddish.__file__)
PY
```

For monorepo GitHub installs, use the package subdirectory form:

```bash
uv pip install 'git+https://github.com/abundant-ai/oddish.git@<ref>#subdirectory=oddish'
```
