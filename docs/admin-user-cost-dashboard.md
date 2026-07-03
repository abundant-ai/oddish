# Admin User Cost Dashboard — Architecture

Branch: `feat/admin-user-cost-dashboard` (off `feat/user-quotas-mvp`).

## Goal

Admins can drill into any user's cost: total spend, per-task breakdown, and a
cost-over-time chart, over a selectable time period. Builds on the quotas MVP
(`billed_user_id`) and the existing admin Costs tab. 80/20: one new backend
endpoint, one new frontend page, links from two existing tables. No new
tables, columns, or migrations.

## Context (what exists)

- `GET /admin/costs` (`backend/api/routers/admin.py:168`,
  `get_cost_breakdown_core` in `oddish/src/oddish/core/admin.py:1405`):
  global (no org filter), `require_admin`, `window_days=0` = all-time.
  Breakdown by user/model/experiment + time series, attributed via
  `experiments.owner_user_id`, bucketed on `trials.created_at`.
- Quotas MVP: `trials.billed_user_id` — denormalized payer stamped at sweep
  time by `resolve_billed_user_id`
  (`backend/api/routers/task_submission.py:260`, github_id precedence, then
  api-key/submitter). Spend summed over `finished_at`
  (`oddish/src/oddish/core/quotas.py:39`), **native `cost_usd` only, deleted
  trials included**, backed by `idx_trials_org_billed_user_finished`.
- Cost rule (`oddish/src/oddish/core/admin.py:1025`): per trial, native
  `cost_usd` when present, else `estimate_cost_usd(model, tokens)` — mutually
  exclusive; reported as separate `cost_usd` / `cost_estimated_usd` fields.
- `trials.task_id` → `TaskModel` relationship exists with `idx_trials_task_id`.
- `UserModel.org_id` gives every cloud user a single org.
- Frontend: `/admin` tabbed page; Costs tab (`cost-breakdown-card.tsx`) has a
  1/7/30/90/all-days `Select`, Recharts `CostChart` (module-local, not
  exported), and a `UserTable`; Quotas tab (`quota-admin-form.tsx`) lists
  members with used-today.

## Decisions

1. **Attribution basis: `billed_user_id`.** Billing truth (quota invariant),
   denormalized on every trial. `billed_user_id IS NULL` trials draw down
   nobody and never appear in this view (the equality filter excludes them by
   construction — do not surface an "unattributed" bucket).
2. **Settled trials only, always.** Every query carries
   `finished_at IS NOT NULL`, including all-time windows (in-flight trials
   have NULL `finished_at` and no cost yet). Time axis is `finished_at`.
3. **Soft-deleted trials are included**, mirroring quota spend semantics
   (`quotas.py` bypasses the default `deleted_at IS NULL` ORM filter —
   copy that pattern; do not add a `deleted_at` predicate, and note the
   backing index is not partial).
4. **Estimates included, divergence from quota `used_usd` accepted.** Quota
   spend sums native `cost_usd` only; this view adds token-based estimates for
   trials without native cost, split into `cost_usd` / `cost_estimated_usd`
   (same as the Costs tab). The drilldown total can therefore exceed the
   Quotas tab `used_usd` for the same user/day — deliberate; the UI keeps the
   existing estimated-cost marker so the split is visible.
5. **Preset windows only** (1/7/30/90/all days), `window_days=0` = all-time,
   same `Query(7, ge=0, le=3650)` semantics as `/admin/costs`.
6. **Task is the leaf.** Group by `task_id`; no per-trial rows in this view.
7. **Attribution basis matches the Costs tab.** Originally the Costs tab
   `by_user` table was keyed by `owner_user_id` while the drilldown was
   billed-basis; that mismatch was accepted for the first cut and the
   migration deferred. The follow-up has since landed: `/admin/costs`
   `by_user` and `series_by_user` are billed-basis too (`billed_user_id`,
   wire field renamed accordingly). Pre-rollout trials have no payer (no
   backfill) and fold into "Unattributed" on longer windows, and the
   unattributed row carries no org (it spans orgs). The experiments table
   stays owner-basis. Remaining drilldown-vs-Costs-tab divergence is the
   time axis: settled-only `finished_at` (deleted included) here vs
   `created_at` buckets (deleted excluded) there.

## Backend

### Core query + response models — `oddish/src/oddish/core/admin.py`

New `get_user_cost_breakdown_core(session, *, org_id, billed_user_id,
window_days, task_limit)`:

- Filter: `org_id`, `billed_user_id`, `finished_at IS NOT NULL`, and
  `finished_at >= window_start` when `window_days` is set. Rides
  `idx_trials_org_billed_user_finished`.
- Aggregate in SQL grouped by `(task_id, model, provider)` — model is needed
  for pricing estimates, provider carried for display — then roll up per task
  in Python, mirroring `get_cost_breakdown_core`. Never load trial rows.
- Per-task row: `task_id`, `task_name` (join `TaskModel`), `trial_count`,
  `cost_usd`, `cost_estimated_usd`, `models[]`. Sorted by total cost desc,
  capped at `task_limit` (default 100), with `task_count` in totals so the UI
  can say "top N of M".
- Series: `date_trunc(bucket, finished_at)` via the existing
  `_series_bucket(window_days)`, segmented by model, emitting the **exact
  `CostSeries` shape** (`keys`, `buckets[].costs[key]`) so the frontend
  `CostChart` drops in unchanged.
- Totals: `cost_usd`, `cost_estimated_usd`, `trial_count`, `task_count`.
- `UserCostBreakdownResponse` (pydantic) lives here, next to
  `CostBreakdownResponse` (`admin.py:1147`) — the router already imports its
  response models from core.

### Router — `backend/api/routers/admin.py`

`GET /admin/costs/users/{user_id}?window_days=7&task_limit=100`,
`Depends(require_admin)` (consistent with `/admin/costs`; FULL API keys
allowed — this is the admin costs surface, not quota management).

- Resolve the target `UserModel` by id: **404 only if the user id is
  unknown**; a known user with zero trials returns an empty breakdown
  (totals zero, empty tasks/series) — mirrors `GET /quotas` listing zero-usage
  members.
- Pass the target user's own `org_id` to the core query (this is what makes
  the composite index usable; caller org is irrelevant since admin is global).
- Fill the user's display identity (name/email/github) with a small direct
  `UserModel` lookup — do **not** call `_enrich_cost_breakdown`; it mutates a
  full `CostBreakdownResponse` and doesn't fit a single-user payload.

## Frontend

### New page — `frontend/src/app/(app)/admin/users/[userId]/page.tsx`

`"use client"`, SWR + `fetcher` (`src/lib/api.ts`) against
`/api/admin/users/{userId}/costs?window_days=N`. Layout, top to bottom:

- Header: user name/email, back link to `/admin`, muted caption
  "billed-user attribution" (see Decision 7).
- Period `Select` with the existing presets.
- Totals row: total cost (`formatCostUsd`, estimated-cost marker as in the
  Costs tab), trial count, task count.
- Chart: stacked-by-model bars over time — **export `CostChart` from
  `cost-breakdown-card.tsx`** (it is module-local today) and reuse; do not
  fork it.
- Per-task table (`ui/table.tsx`): task name (link to the task's existing
  page under `/tasks` where applicable), trials, models, cost; "top N of M"
  note when `task_count > task_limit`.
- Loading `Skeleton` / destructive `Alert` on error (403 → "Check if you have
  admin access.") / empty state, per existing admin-card conventions.

### Proxy route — `frontend/src/app/api/admin/users/[userId]/costs/route.ts`

`proxyBackendJson({ path: "admin/costs/users/{userId}?..." })`, same as the
other `/api/admin/*` routes.

### Entry points

- Costs tab `UserTable`: user cell links to `/admin/users/{userId}`
  (basis caveat per Decision 7).
- Quotas tab `QuotaAdminForm`: member name links to the same page.

## Testing

- Backend pytest (requires_db pattern): attribution filter (only the target
  user's billed trials count; NULL `billed_user_id` excluded), settled-only
  (`finished_at IS NULL` excluded even all-time), soft-deleted trials
  included, `finished_at` window filtering, native-vs-estimate split,
  per-task grouping and `task_limit` cap, zero-trial user → empty 200,
  unknown user → 404, non-admin → 403.
- Frontend: no wired test suite; manual verification.

## Out of scope (deliberate)

- Calendar date ranges; CSV export; per-trial drilldown; org switcher.
- Reserved/in-flight cost in this view.
- An "unattributed" (NULL `billed_user_id`) bucket *in the drilldown* (the
  Costs tab's by-user table does keep its "Unattributed" row; see Decision 7).
- Any change to quota admission/enforcement behavior — the quotas MVP's edge
  cases are deliberate; this feature is read-only over its data.
