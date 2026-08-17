---
name: oddish
description: Run, monitor, QA, and pull Harbor evaluation tasks with the oddish CLI. Use when submitting eval sweeps (agents/models/nop/oracle baselines), checking trial progress, triggering or reading task QA verdicts and findings, diagnosing failures, downloading logs/artifacts, or iterating on a local task. Requires the oddish package installed and ODDISH_API_KEY set.
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
  missing/failed/unexpected baseline makes the gate `faulty` and model trials
  land as `skipped` — that is the task failing admission, not the model failing.
- **QA is task-scoped, not trial-scoped.** One QA job classifies every live
  trial of a task, then synthesizes the task verdict. You trigger and read it
  per task.
- **`oddish review` is read-only.** It never enqueues analysis or spends money.
  Triggering QA is always a separate, explicit command.
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

Triggering and reading are separate commands. QA classifies stored trajectories;
it never re-executes trials.

```bash
# 1. Trigger (mutating, LLM cost). Pick ONE:
oddish backfill-analysis --task <task_id>          # fills unanalyzed trials + recomputes verdict
oddish backfill-analysis --task <task_id> --force  # redo already-analyzed trials too
oddish run <task_id> --retry --qa -y               # re-run the whole task QA job

# 2. Read (read-only). --wait blocks until the active QA run settles.
oddish review <task_id> --wait --json
```

- `review --wait` polls and never starts analysis itself; `--wait-timeout N`
  bounds it (default 900s). On timeout it warns on stderr and still emits the
  current document — check `qa.status` / `qa.active_run` before trusting it.
- Narrow to a version or experiment: `--version N`, `--experiment <id>`.
- Filter findings: `--tier must_fix --tier should_fix` (repeatable; omit for all).
- CI gate: `oddish review <task_id> --tier must_fix --json --fail-on-findings`
  exits `2` when the selected tiers contain findings.

`oddish review` exit codes: `0` read OK · `1` auth/transport/lookup/validation
failure · `2` `--fail-on-findings` matched.

`review --json` emits one complete document (schema_version 1). Key fields:

```json
{
  "task": {"id": "...", "name": "...", "version": 18},
  "qa": {"status": "success", "result_run": {"disposition": "published"}},
  "baselines": {"outcome": "valid", "nop": {"valid": true}, "oracle": {"valid": true}},
  "verdict": {"verdict": "reject", "confidence": "high", "primary_issue": "..."},
  "findings": [{"id": "...", "tier": "must_fix", "file": "...", "title": "...",
                "detail": "...", "recommendation": "..."}],
  "trials": [{"id": "...", "role": "model", "reward": 0,
              "analysis": {"classification": "GOOD_FAILURE"}}]
}
```

Reading it correctly:

- Keep **verifier reward** and **QA classification** separate. A `GOOD_FAILURE`
  trial still failed the verifier; a `BAD_SUCCESS` trial passed the verifier but
  QA suspects the pass (e.g. reward hacking).
- `baselines.outcome` of `faulty` invalidates model results — fix the task's
  baselines before reading anything into model rewards.
- A verdict predating version-owned provenance is reported as legacy and is
  never guessed onto the selected version; re-run QA to get scoped provenance.

## Workflow 4 — Pull evidence (read-only)

```bash
oddish pull <trial_id> --structured --files --out /tmp/<trial_id>
oddish pull <task_id>                # whole task; experiment IDs also work
```

Read in order: `result.json` → verifier `reward.json` / `details.json` →
`trial.log` → trajectory / agent log. Live transcripts (`oddish logs`) are
purged when a trial ends; `pull` fetches the permanent S3 record.

## Workflow 5 — Retry, cancel, iterate

```bash
oddish run <trial_id> --retry -y            # re-run one failed trial
oddish run <task_id> --retry -y             # re-run every failed trial in a task
oddish cancel <task_id>                     # stop in-flight trials (completed kept)
oddish cancel <task_id> --qa                # stop only the in-flight QA job
oddish iterate ./my-task -a codex -m openai/gpt-5.2   # local authoring loop:
                                            # upload → gated baselines → pilot → QA → compare
```

- `iterate` exit codes: `0` accepted · `1` infrastructure/execution failure ·
  `2` task needs author attention (faulty baselines or rejected verdict).
- Diagnose before cancelling: `cancel` stamps "Cancelled by user", which hides
  the earlier exception.

## Gotchas

- `--qa` requires `--retry` (`oddish run <id> --retry --qa`).
- `--json` on `run` implies `--background` — no watch output follows.
- Trial IDs are `<task_id>-<index>`; commands that take a task usually accept a
  trial ID and resolve to its parent task.
- `oddish review` requires an exact task ID or exact org-unique task name — no
  fuzzy matching.
- Baseline `nop` scoring `0` is the EXPECTED calibration result, not a failure.
  Do not judge task quality from an average reward that includes nop.
- `status --queue` and `costs` need a full-scope API key on hosted Oddish.
- Task/experiment `delete` is self-host only; hosted supports `--trial` deletes.
  Never delete without explicit user authorization.
- Full command reference: `oddish <command> --help` (always current with the
  installed package) or https://github.com/abundant-ai/oddish/blob/main/DOCS.md
