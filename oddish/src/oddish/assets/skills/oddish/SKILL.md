---
name: oddish
description: Run, inspect, compare, retry, and diagnose Harbor evaluation tasks through the Oddish CLI. Use for Oddish task versions, experiments, agent trials, nop/oracle baseline gates, task-level QA verdicts, trajectory summaries, logs, artifacts, and preflight checks. Do not use for a plain local Harbor run that will not be submitted to Oddish.
---

# Oddish evaluations

Oddish is the scheduler and results store around Harbor, the task-execution
harness. Use the installed CLI as the executable contract and this skill for
the repository rules that are easy to misread from command output.

## Start safely

```bash
oddish --help
oddish status --json
```

API-backed commands require `ODDISH_API_KEY`. Never print, log, commit, or
return its value. The API target resolves in this order:
`ODDISH_API_URL`, `ODDISH_PREVIEW_PR`, then hosted Oddish.

Run `oddish <command> --help` before relying on an option not shown here.
`--json` exists on many operational commands, but it is not a global option.
`oddish logs`, `oddish link`, and `oddish probe` do not provide JSON output.

## Authorization boundaries

Treat these as separate mutations:

- `run`, trial retry, and QA backfill can spend money. Get authorization for
  the intended agents, models, trial count, and scope.
- `publish` and `collect` can expose an experiment through a public link. Get
  authorization before making data public.
- `cancel` stops active work. Inspect the failure first because cancellation
  replaces the visible error with `Cancelled by user`.
- `delete` removes database visibility. Get explicit authorization for the
  exact trial, task, or experiment.

Read-only inspection through `status`, `ls`, `logs`, `pull`, and `link` does
not authorize a later mutation.

## Normal workflow

1. Validate a local task before submitting it:

   ```bash
   oddish preflight ./task --json
   ```

   `run` and task-mode `upload` perform the same gate. Use `--force` only when
   the user accepts the reported finding.

2. Submit all comparison columns against one task version and one experiment:

   ```bash
   oddish run ./task -c sweep.yaml --json
   ```

   Include nop, oracle, and paid model trials in the same sweep. `run --json`
   implies background mode. Preserve `tasks[].id` and `experiment_url` from
   the output; the `experiment` field is the experiment name, not a guaranteed
   identifier.

3. Inspect a task, experiment, or individual trial:

   ```bash
   oddish status <task_id> --json
   oddish status --experiment <experiment_id> --json
   oddish status <trial_id> --json
   oddish logs <trial_id> --follow
   ```

   Task JSON can contain platform analysis trials. Count evaluation attempts
   only where `trials[].kind == "agent"`.

4. Let task-level QA start automatically after current-version agent trials
   settle and the pre-trial audit finishes. Read the task verdict from task
   status and the full classification/action items from individual trial
   status. Trigger a replacement pass only when requested:

   ```bash
   oddish backfill-analysis --task <task_id> --json
   oddish run <task_id> --retry --qa --yes --json
   ```

5. Pull permanent evidence before diagnosing or cancelling:

   ```bash
   oddish pull <trial_id> --structured --files --out /tmp/<trial_id>
   ```

   Read the result and verifier artifacts before the log and trajectory.
   Live transcript events are temporary; pulled storage artifacts are the
   permanent record.

6. Retry only the intended immutable attempt:

   ```bash
   oddish run <trial_id> --retry --yes --json
   oddish run <task_id> --retry --yes --json
   ```

   A retry creates a new trial row and points the old row to it through
   `superseded_by_trial_id`; it does not rewrite the old attempt.

## Load the contract you need

- Read [references/domain-contract.md](references/domain-contract.md) before
  comparing versions, counting trials, interpreting statuses, using baselines,
  or retrying work.
- Read [references/qa-contract.md](references/qa-contract.md) before reading or
  rerunning audits, classifications, verdicts, action items, or summaries.
- Read [references/cli-contract.md](references/cli-contract.md) before scripting
  commands, parsing JSON, choosing an API target, or checking API-key scope.
- Read [references/known-contract-traps.md](references/known-contract-traps.md)
  when repository docs, comments, UI labels, and runtime behavior disagree.

For details beyond these contracts, prefer the current runtime enum,
predicate, response schema, endpoint, or Typer command definition over prose
documentation. `AGENTS.md` is the architecture guide; `DOCS.md` is the end-user
CLI guide. Plans and handoff notes describe proposed or historical work, not
the running contract.
