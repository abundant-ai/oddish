# Oddish Logfire operations rollout

This file tracks the code, Logfire, and verification work required to finish
PR #1366. Check a box only after recording the requested evidence. Never paste
the `LOGFIRE_TOKEN` value into this file, GitHub, Slack, or a chat.

## Current state

- Worktree: `/Users/kyle/Desktop/oddish/.worktrees/logfire-operations-metrics`
- Branch: `codex/logfire-operations-metrics`
- Pull request: `https://github.com/abundant-ai/oddish/pull/1366`
- Target Logfire dashboard: `Oddish Operations`
- Preferred smoke-test environment: `preview-pr-1366`
- Alternative smoke-test environment: `staging`

## Repository work (Codex)

- [x] Fast-forward the worktree to the current PR head.
- [x] Remove the unrelated GKE preview-teardown changes from the worktree diff.
- [x] Define dispatch outcomes as `success`, `skipped`, and `error` in the
  shared Oddish observability code.
- [x] Record a handled transient `OSError` as `skipped` in the hosted Modal
  dispatcher and the standalone dispatcher.
- [x] Keep worker transition metrics after the accepted PostgreSQL state change.
- [x] Rename the dashboard ratios to `retrying attempt share` and
  `terminal-failure attempt share`.
- [x] Add dispatch-outcome and dispatcher-error-percentage panels.
- [x] Add SQL for dispatcher errors, terminal attempt failures, queue
  saturation, and missing dispatcher telemetry alerts.
- [x] Update the Logfire setup and verification runbook.
- [x] Run targeted Python tests, Ruff, and `git diff --check`.
  - Backend dispatcher and preview workflow: `50 passed in 17.51s`.
  - Core dispatch, metrics, worker outcome, and race tests:
    `55 passed in 1.18s`.
  - Post-format recorder tests: `8 passed in 1.26s`.
  - Ruff 0.8.4 imports/unused variables: `All checks passed!`.
  - Ruff 0.8.4 format check: `9 files already formatted`.
  - Logfire API contract: installed version `4.33.0`; all three factory and
    observation call signatures match.
  - SQL syntax: `4` alert statements and `10` dashboard statements parsed.
  - `git diff --check`: exited zero with no output.
- [ ] Record the final changed-file and line-count report.
- [ ] Update the PR title/body with observed verification output.

## Logfire setup (Kyle)

- [ ] Open the Logfire project that should receive Oddish telemetry.
- [ ] Create or select a project write token.
- [ ] In Modal's `main` environment, edit the `oddish-prod` secret.
- [ ] Add `LOGFIRE_TOKEN=<Logfire project write token>` without copying the
  value into this file or Git.
- [ ] Record only that the token is configured:
  - Configured: `yes / no`
  - Date checked: `YYYY-MM-DD`
- [ ] Choose the smoke-test environment:
  - Selected environment: `preview-pr-1366 / staging`
- [ ] Configure a Logfire notification channel if alerts should notify people:
  - Channel type: `Slack / email / other / none`
  - Channel label, without credentials: `<label>`

## Ingestion verification (Kyle after deploy)

Run this in Logfire SQL Workbench:

```sql
SELECT
    metric_name,
    service_name,
    deployment_environment,
    COUNT(*) AS data_points
FROM metrics
WHERE metric_name LIKE 'oddish.%'
GROUP BY metric_name, service_name, deployment_environment
ORDER BY metric_name;
```

Expected metric names:

- `oddish.dispatch.cycles`
- `oddish.dispatch.duration`
- `oddish.dispatch.workers_spawned`
- `oddish.queue.jobs`
- `oddish.queue.slots`
- `oddish.worker_job.duration`
- `oddish.worker_job.transitions`

Record non-secret evidence here:

- [ ] All seven metric names appeared.
- `service_name`: `<observed value>`
- `deployment_environment`: `<observed value>`
- Query error, or `none`: `<text>`
- Evidence timestamp: `YYYY-MM-DD HH:MM timezone`

## Dashboard setup (Kyle)

- [ ] Create the custom dashboard `Oddish Operations`.
- [ ] Add list variable `deployment_environment` using observed values.
- [ ] Add list variable `service_name` using observed values.
- [ ] Add list variable `queue_key` with `__all__` and observed queue keys.
- [ ] Add list variable `worker_job_kind` with `__all__`, `TRIAL`,
  `TASK_EXPAND`, and `TAG_PROJECT`.
- [ ] Add panel 1: queued and running jobs by queue.
- [ ] Add panel 2: held queue slots versus configured limits.
- [ ] Add panel 3: worker attempt transitions by outcome.
- [ ] Add panel 4: retrying attempt share.
- [ ] Add panel 5: terminal-failure attempt share.
- [ ] Add panel 6: P50 and P95 worker-attempt duration.
- [ ] Add panel 7: workers spawned per successful cycle.
- [ ] Add panel 8: cycles reaching the spawn cap.
- [ ] Add panel 9: dispatch cycles by `success`, `skipped`, and `error`.
- [ ] Add panel 10: dispatcher error percentage.
- [ ] Confirm all ten panels run without SQL errors against real Oddish data.
- [ ] Enable Logfire's standard `Web Server Metrics` dashboard.
- [ ] Enable Logfire's standard `Basic System Metrics` dashboard.

## Alert setup (Kyle)

Use the checked-in alert SQL. Record the chosen operational policy without
recording webhook URLs, tokens, or recipient addresses.

- [ ] Create the unexpected dispatcher errors alert.
- [ ] Create the sustained terminal attempt failures alert.
- [ ] Create the sustained queue saturation alert.
- [ ] Create the missing dispatcher telemetry alert.
- Evaluation interval: `<value>`
- Lookback window: `<value>`
- Thresholds: `<values>`
- Notification channel label: `<label>`
- Notification schedule: `<value>`
- Query errors, or `none`: `<text>`

## Dashboard export (Kyle, then Codex)

- [ ] In Logfire, use `Download dashboard as code` after all ten panels render.
- [ ] Save the unedited export at
  `/Users/kyle/Desktop/oddish/.worktrees/logfire-operations-metrics/docs/observability/logfire-oddish-operations.json`.
- [ ] Tell Codex that the file is present.
- [ ] Codex validates that the JSON contains all ten panels and four variables.
- [ ] Codex commits the exported JSON.
- [ ] Kyle imports the committed JSON into a second/test project or otherwise
  confirms that Logfire accepts the export.

## Completion evidence

- [ ] After commit/push, PR #1366 contains no GKE preview-teardown diff.
- [ ] A transient dispatch `OSError` appears as `outcome = 'skipped'`.
- [ ] An unexpected dispatcher failure appears as `outcome = 'error'`.
- [ ] All seven metrics arrive in the chosen Logfire environment.
- [ ] All ten dashboard panels return data without SQL errors.
- [ ] All four alert queries evaluate without SQL errors.
- [ ] The exported dashboard JSON imports successfully.
- [ ] Targeted pytest output is recorded in the PR.
- [ ] Ruff output is recorded in the PR.
- [ ] `git diff --check` returns no output and exits zero.
- [ ] No `frontend/`, API-route, database-model, or migration changes appear in
  the final PR diff.
