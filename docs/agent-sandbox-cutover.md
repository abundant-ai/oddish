# Agent-Sandbox Cutover

Operational reference for the `feat/agent-sandbox-cutover` branch. Covers what changed, how to verify it works, and how to undo it cleanly.

---

## What the cutover does

Oddish's probe-dispatch and cc-chat handlers now proxy to the external `agent-sandbox-service` instead of running locally. The local `cc_chat` orchestrator has been deleted from this repo; harbor's probe-dispatch path has been removed from the worker. A new `external_probe_runs` table in the oddish DB tracks runs that were dispatched to the service, so the workbench history can surface them alongside legacy trial rows.

---

## Prerequisites for deployment

All of the following must be true before this branch is merged and deployed:

- **`feat/agent-sandbox-endpoints` merged to oddish `main`.** This branch depends on the Phase A endpoints being present on the agent-sandbox-service side.
- **`agent-sandbox-service` deployed and reachable.** In prod, this is Railway. In local dev, run it on localhost at whatever port the service listens on.
- **Environment variables set in oddish's backend env:**

  ```bash
  ODDISH_AGENT_SANDBOX_SERVICE_URL=https://<service-host>
  ODDISH_AGENT_SANDBOX_SERVICE_API_KEY=<service-api-key>
  ```

- **Alembic migration applied to the oddish DB:**

  ```bash
  cd oddish
  uv run alembic upgrade head   # applies a4b5c6d7e8f9_add_external_probe_runs.py
  ```

---

## Smoke procedure (D1)

Run this end-to-end against staging before promoting to production.

### 1. Deploy agent-sandbox-service to staging

Deploy or confirm the service is running and healthy at the staging URL. Verify `GET /healthz` (or equivalent) returns 200.

### 2. Deploy oddish (this branch) to staging

Set `ODDISH_AGENT_SANDBOX_SERVICE_URL` and `ODDISH_AGENT_SANDBOX_SERVICE_API_KEY` in the staging environment before deploying.

### 3. Apply the migration

```bash
cd oddish
uv run alembic upgrade head
```

Confirm the `external_probe_runs` table exists in the staging DB before proceeding.

### 4. Smoke the probe path

From the staging frontend workbench:

1. Submit a probe on any recent task.
2. Verify a history row appears with a `pr_*` run ID and **no** "legacy" badge.
3. Watch the row status transition: `queued` → `running` → `succeeded` over the next few minutes.
4. Verify the truncated `pr_*` ID is visible in the result column.

> The detail page click-through for service-routed rows is a v2 follow-up — clicking the row may 404; that is expected.

### 5. Smoke the cc-chat path

From the staging frontend, open a cc-chat session on a recent experiment:

1. Verify the session opens within 10–20 s.
2. Send a first message and verify stream-JSON events arrive.
3. Close the session.
4. After close, confirm the transcript is downloadable:

   ```
   GET /api/.../cc-session/{id}
   ```

### 6. If anything fails

- Capture logs from both oddish and agent-sandbox-service.
- Revert this deploy to the previous mainline commit (see [Rollback](#rollback-procedure-d2)).
- File an issue before re-attempting.

---

## Rollback procedure (D2)

Use this if a critical bug surfaces after deploying this branch to production.

### Step 1 — Revert the deploy

Redeploy the previous mainline commit (the `feat/freeform-plus-cc-chat` baseline). The local `cc_chat` orchestrator and the harbor probe-dispatch path come back wholesale because they were never deleted from that commit.

No schema changes need to be reverted immediately — the `external_probe_runs` table is additive and causes no harm if left in place.

### What survives a rollback

| Data | State after rollback |
|---|---|
| Pre-cutover trial rows (`trials` table) | Fully readable. Untouched by this branch. |
| Post-cutover service-routed probe runs | Still queryable directly via `GET /v1/probes/{id}` on the agent-sandbox-service (use the service API key). Not visible in the rollback'd workbench because the rollback'd oddish doesn't read `external_probe_runs`. |

Rollback is **mostly safe** — no data is destroyed. The only loss is workbench history visibility for probe runs dispatched after this branch went live. If the rollback window is short, this is acceptable. If it drags on, those rows can be backfilled into `trials` manually.

---

## Known v1 limitations

- **No detail page for service-routed probe rows.** The workbench history shows the truncated `pr_*` ID but the row is not clickable. A future task wires a `/tasks/{task_id}/probe/service/{run_id}` detail route.

- **`list_task_probe_runs` legacy path does a Python-side filter.** It SELECTs all trials and filters by `harbor_config.mode == "probe"` in Python. Fine at current volume; push to a JSONB SQL filter if volume grows.

- **No `delete_sandbox_by_id` on the service.** The cc-chat session restart sweep and the probe-runner restart sweep both rely on Daytona's `auto_stop_minutes` to reap orphaned sandboxes rather than explicit teardown calls. Same caveat applies to both paths.

---

## Carryover items from earlier plans

The agent-sandbox-service itself has two cleanup items carried over from the Phase A/B work:

- **Naive datetime columns** — timestamps are stored without timezone info; should be migrated to `TIMESTAMPTZ`.
- **`delete_sandbox_by_id` not implemented** — blocks explicit sandbox teardown (see limitation above).

Neither item blocks this cutover, but both should be addressed before v2 detail-page work begins.
