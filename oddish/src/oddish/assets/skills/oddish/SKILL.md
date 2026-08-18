---
name: oddish
description: Run, monitor, QA, and pull Harbor evaluation tasks with the oddish CLI. Use when submitting eval sweeps (agents/models/nop/oracle baselines), checking trial progress, triggering or reading task QA verdicts and per-trial classifications, diagnosing failures, downloading logs/artifacts, or retrying cancelled work. Requires the oddish package installed and ODDISH_API_KEY set.
---

# oddish — drive Harbor evals from the CLI

Oddish schedules Harbor-compatible eval trials against an API server, tracks them
through execution, and runs task-level QA (LLM trajectory classification + a task
verdict). This skill teaches an agent the deterministic command paths.

## Setup

```bash
oddish --help                 # confirms the CLI is installed
export ODDISH_API_KEY="ok_..."  # required for every API call
oddish status                 # read-only; confirms auth + connectivity
```

- Never print, log, commit, or reply with the value of `ODDISH_API_KEY`.
- `ODDISH_API_URL` overrides the target server; unset means the hosted API.
- **Every command except `oddish logs` accepts `--json`** for machine-readable
  output. Agents should pass `--json` and parse stdout; human output is Rich
  tables and not a stable contract.

## Core invariants

- **One comparison = one frozen task version.** Submit nop, oracle, and every
  model in ONE sweep against ONE task version. Do not append a model-only
  submission after the task version changed — the old baseline columns stop
  applying.
- **Baseline gating.** When a sweep includes baselines, `nop` must finish with
  reward `0` and `oracle` with reward `1` before paid model trials run. A
  missing/failed/unexpected baseline makes the gate fail and model trials land
  as `skipped` — that is the task failing admission, not the model failing.
- **QA is task-scoped, not trial-scoped.** One QA job classifies every live
  trial of a task, then synthesizes the task verdict. You trigger and read it
  per task.
- **Re-submitting the same sweep reconciles.** Live/successful trials satisfy
  the requested count; failed ones are replaced (old attempts kept for history,
  marked superseded). Interrupted submissions are safe to repeat.

## Workflow 1 — Submit a sweep (mutating, spends money)

```bash
# Single task, one agent/model
oddish run ./my-task -a claude-code -m anthropic/claude-sonnet-4-5 --n-trials 5 --json

# Multi-agent comparison with baselines, from config
oddish run ./my-task -c sweep.yaml --json
```

`sweep.yaml` shape (nop + oracle + models in one atomic sweep):

```yaml
agents:
  - name: nop
    model_name: none/none
    n_trials: 1
  - name: oracle
    model_name: none/none
    n_trials: 1
  - name: claude-code
    model_name: anthropic/claude-sonnet-4-5
    n_trials: 3
```

- `--json` implies `--background`: submit returns immediately with IDs.
  Record the task ID(s) and experiment ID from the output.
- Add `--run-analysis` to auto-enqueue QA once every trial of a task finishes.
  Without it, trigger QA manually later (Workflow 3).
- Only submit when the user has authorized a paid run.

## Workflow 2 — Monitor (read-only)

```bash
oddish status <task_id> --json        # single JSON snapshot for scripts/agents
oddish status --experiment <id>       # experiment-level progress
oddish logs <trial_id> --follow       # live transcript of a running trial
```

Interpret `status` / `harbor_stage` literally: `queued` (no worker yet),
`environment setup`, `agent running`, `verification`, `retrying` (inspect the
exception before cancelling — do not cancel just because setup takes minutes).
Terminal trial statuses: `success`, `failed`, `cancelled`, `skipped`.

## Workflow 3 — Trigger QA after trials finish, then read it

QA classifies stored trajectories; it never re-executes trials. Triggering and
reading are separate commands.

```bash
# 1. Trigger (mutating, LLM cost). Pick ONE:
oddish backfill-analysis --task <task_id>          # fills unanalyzed trials + recomputes verdict
oddish backfill-analysis --task <task_id> --force  # redo already-analyzed trials too
oddish run <task_id> --retry --qa -y               # re-run the whole task QA job

# 2. Read (read-only). Poll until verdict_status is success or failed.
oddish status <task_id> --json
```

Read these fields from the task JSON:

- `verdict_status` — `queued` / `running` (QA in flight; poll again in a few
  seconds), `success` / `failed` (settled).
- `verdict` — the settled task verdict: `verdict` (`accept` / `reject`),
  `is_good`, `confidence`, `primary_issue`, `reasoning`, and
  **`recommendations` — the task-level fix list** (3–5 items for rejected
  tasks). The verdict carries no per-finding details; those live on trials.
- Per-trial `analysis` — TRIMMED here to `classification` / `subtype` /
  `evidence`. Classifications: `GOOD_SUCCESS`, `GOOD_FAILURE`, `BAD_SUCCESS`,
  `BAD_FAILURE`, `HARNESS_ERROR`.

The concrete fix suggestions are one level down, on the full trial record:

```bash
oddish status <trial_id> --json      # <task_id>-<index>
```

Its full `analysis` adds `recommendation` (per-trial fix) and
`action_items[]` — findings with `file`, `line_start`–`line_end`, `title`,
`detail`, **`recommendation`** (the fix), `tier` (`must_fix` / `should_fix` /
`optional`), `exploited` + `exploit_evidence`, and `source` (`pre_trial` =
found by the source audit before any run, `post_trial` = found from
trajectories). Everything the web UI's QA panel renders is these stored
fields; nothing is UI-only.

Reading it correctly:

- Keep **verifier reward** and **QA classification** separate. A `GOOD_FAILURE`
  trial still failed the verifier; a `BAD_SUCCESS` trial passed the verifier but
  QA suspects the pass (e.g. reward hacking).
- If baselines gated the sweep (`skipped` model trials), fix the task's
  baselines before reading anything into model results.

## Workflow 4 — Pull evidence (read-only)

```bash
oddish pull <trial_id> --structured --files --out /tmp/<trial_id>
oddish pull <task_id>                # whole task; experiment IDs also work
```

Read in order: `result.json` → verifier `reward.json` / `details.json` →
`trial.log` → trajectory / agent log. Live transcripts (`oddish logs`) are
purged when a trial ends; `pull` fetches the permanent S3 record.

## Workflow 5 — Retry and cancel

```bash
oddish run <trial_id> --retry -y            # re-run one failed trial
oddish run <task_id> --retry -y             # re-run every failed trial in a task
oddish cancel <task_id>                     # stop in-flight trials (completed kept)
oddish cancel <task_id> --qa                # stop only the in-flight QA job
```

Diagnose before cancelling: `cancel` stamps "Cancelled by user", which hides
the earlier exception. Pull the trial first.

## Gotchas

- `--qa` requires `--retry` (`oddish run <id> --retry --qa`).
- `--json` on `run` implies `--background` — no watch output follows.
- Trial IDs are `<task_id>-<index>`; commands that take a task usually accept a
  trial ID and resolve to its parent task.
- Baseline `nop` scoring `0` is the EXPECTED calibration result, not a failure.
  Do not judge task quality from an average reward that includes nop.
- `status --queue` and `costs` need a full-scope API key on hosted Oddish.
- Task/experiment `delete` is self-host only; hosted supports `--trial` deletes.
  Never delete without explicit user authorization.
- Full command reference: `oddish <command> --help` (always current with the
  installed package) or https://github.com/abundant-ai/oddish/blob/main/DOCS.md
