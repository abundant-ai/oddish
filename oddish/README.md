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

Run `oddish --help` or see [`../DOCS.md`](../DOCS.md) for the full CLI
reference. The main commands are:

- `oddish run` — submit local tasks, registry datasets, sweeps, retries, and task-level QA retries.
- `oddish upload` — register task bundles or import off-oddish Harbor trial results.
- `oddish ls` / `oddish status` — browse tasks (including model and trajectory-metric filters) and inspect progress. `oddish status <trial_id>` shows single-trial detail; `--detail`/`--versions` show a task's version history and cost rollups; `--queue` shows queue & worker scheduler diagnostics.
- `oddish logs` — stream a running trial's live transcript and cost estimate (`--follow` to poll until it ends); finished trials are served by `oddish pull` instead.
- `oddish costs` — billable-spend accounting (org-wide, or per-user with `--user`).
- `oddish cancel` — cancel active runs or task-level QA.
- `oddish pull` — download logs, results, trajectories, and artifacts; `--debug-files` lists a trial's raw S3 inventory instead.
- `oddish combine` — merge finished trials from multiple experiments.
- `oddish collect` / `oddish experiment create` — build read-only trial collections; `collect` can auto-publish a share link.
- `oddish delete` — delete trials (hosted) or tasks/experiments (self-host only).
- `oddish publish` / `oddish unpublish` — toggle public read-only experiment sharing.
- `oddish backfill-analysis` and `oddish probe` — specialized QA/probe tools.

Every command except `oddish logs` supports `--json` for machine-readable output.

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
- Complete CLI reference: [`DOCS.md`](../DOCS.md)
- Self-hosting and deployment: [`../SELF_HOSTING.md`](../SELF_HOSTING.md)
