# Oddish CLI contract

Use this reference before scripting commands, parsing output, selecting the
server, or deciding which API-key scope is sufficient.

## Server and authentication

The API base URL resolves in this order:

1. `ODDISH_API_URL`, a complete URL override.
2. `ODDISH_PREVIEW_PR`, formatted as the hosted pull-request preview URL.
3. the packaged hosted production URL.

API-backed commands require `ODDISH_API_KEY`, including reads. `oddish skill`
and `oddish link task|trial` are local and do not require it.

Hosted API-key scopes form `full > tasks > read`. Reads require `read`.
Normal task submission requires `tasks`. QA mutations (`qa/retry`,
`qa/backfill`, `qa/pre-trial`, analysis rerun, trajectory-summary refresh)
require a `tasks` key that is NOT member-created; only `qa/cancel` accepts a
member-created `tasks` key. Operator/admin surfaces — queue diagnostics, cost
accounting, concurrency, cost exclusions, and every `delete` route — require
`full`, and some queue diagnostics additionally require the configured
operator organization (a `full` key outside it still gets 403 on part of
`status --queue`). The `publish`/`unpublish` commands and `collect
--publish` require `full` — a `tasks` key never works there, whoever created
it. The one place an admin-created `tasks` key can publish is sweep-time
auto-publish (`run --publish` or GitHub-attributed CI runs); a member-created
`tasks` key cannot.

## Command surface

Current top-level commands are `run`, `upload`, `preflight`, `ls`, `status`,
`skill`, `logs`, `cancel`, `backfill-analysis`, `combine`, `costs`,
`cost-exclusions`, `collect`, `qa-eval`, `delete`, `admin`, `experiment`,
`link`, `pull`, `publish`, `unpublish`, and `probe`.

`qa-eval run` requires an admin-created `tasks` key because it queues paid
analysis trials. `qa-eval collect` is read-only and requires `read` scope.

Use `oddish <command> --help` for the exhaustive option list. Important
submission controls include:

- `run --github-id` for immutable GitHub attribution;
- `run --baseline-gate/--no-baseline-gate` for baseline admission;
- `run --max-trial-attempts` for the total attempt budget including the first;
- `run --json`, which implies `--background`;
- `run --force` and `upload --force`, which bypass preflight after showing
  findings;
- `preflight --json` for a read-only machine-readable gate result.

Preflight parses `task.toml` and checks that open internet is justified, the
agent image does not fetch a repository or expose `.git`, solutions are source
rather than patch files, and anti-cheat checks do not depend on brittle source
scanning.

## JSON output

`--json` is per command, not global. Confirm support with help. In particular,
`logs`, `link task`, `link trial`, `probe`, and `probe skill add` do not expose
JSON mode.

`run --json` prints one document and runs in the background. Its stable
high-level fields include:

- `experiment`: experiment name;
- `experiment_url`: authenticated dashboard URL when resolvable;
- `public_experiment_url`: published URL when created;
- `total_trials`;
- `tasks[]`, including each task `id` and submitted `trials_count`.

Do not assume an `experiment_id` field is present merely because
`experiment_url` is present.

`status --json` is a single snapshot even if `--watch` is also supplied:

- `status <trial_id> --json` returns the individual trial detail;
- `status <task_id> --json` returns the raw task response;
- `status --experiment <id> --json` returns `{experiment_id, tasks}`;
- `status --json` without a target returns `{experiments}`.

Task responses may embed `qa`, `audit`, or `summarize` trial rows. Filter
`trials[].kind == "agent"` before counting evaluation attempts.

## Evidence and live data

`oddish logs <trial_id> [--follow]` reads short-lived live transcript events.
Supported live agents are claude-code, codex, cursor-cli, grok-build, tbh,
and mini-swe-agent. Terminal cleanup purges these events; a 24-hour cleanup pass
removes leaks from hard-killed workers.

`oddish pull` downloads the permanent stored record. For a diagnosis, prefer
structured result/verifier artifacts first, then the trial log and trajectory.
`pull` accepts a trial, task, or experiment target and auto-detects the type.

`oddish link task` and `oddish link trial` only construct dashboard URLs. They
do not read the API or mutate the task.
