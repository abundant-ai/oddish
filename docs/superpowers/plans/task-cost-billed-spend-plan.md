# Per-task cost split: Total cost vs Billed spend

**Status:** implemented in this PR. Follow-up to PR #597 (admin cost dashboard billable filter).

## Problem

The per-task cost indicator sums resolved cost (native `cost_usd`, else token
estimate) over every non-superseded trial of the task. Combine copies land
under the **same task id** (`combine_experiments_core` mints `{task_id}-{index}`
rows copying `cost_usd`), and imported trials carry costs we never paid. Both
have `billed_user_id IS NULL`. So the indicator double counts after a combine,
and the task-detail label "Total spent (all versions)" is a lie: it shows
represented cost, not money spent.

Product decision: show **two** numbers per task.

1. **Total cost** — unchanged from today. Sum over all (non-superseded,
   non-probe on browse) trials, copies included. What the assembled results
   represent.
2. **Billed spend** — new. Same sum restricted to `billed_user_id IS NOT NULL`.
   New money drawn from member budgets. Same definition as the admin cost
   dashboard after #597, so the two surfaces reconcile.

No per-task detailed breakdowns (per-user / per-model). Those exist on the
admin dashboard to answer "who/what"; the task indicator answers "how much" —
two scalars suffice.

## Naming

Avoid "cost to train" (this is an evals platform; nothing is trained).

- API fields: keep `cost_usd` / `cost_trial_count` / `cost_has_estimated` /
  `cost_has_native` untouched (zero consumer churn). Add the parallel set
  `billed_cost_usd`, `billed_trial_count`, `billed_has_estimated`,
  `billed_has_native` (default 0/False so old payloads stay valid).
- UI labels: "Total cost" (tooltip: includes combined/imported copies of
  results) and "Billed spend" (tooltip: new spend billed to org members).
  Rename the task-detail stat "Total spent (all versions)" to
  "Total cost (all versions)" — its current wording is the bug.

## Where the numbers come from today

| Surface | Computation | Billable info available? |
|---|---|---|
| Tasks browse card (`frontend/src/app/(app)/tasks/task-card.tsx`) | SQL row scan + Python fold in `list_tasks_browse_core`, `oddish/src/oddish/core/endpoints/tasks_query.py` ~2080–2145 (`_resolve_browse_trial_cost`) | No — add `TrialModel.billed_user_id` to the select |
| Task detail stat cards (`frontend/src/app/(app)/tasks/[task_id]/task-detail-client.tsx` ~865–898) | `_aggregate_task_detail_rollups` in `oddish/src/oddish/core/endpoints/task_detail.py` folding **TrialResponse** objects (no `billed_user_id`) | Yes via `all_trial_models` in `get_task_detail_core` (full ORM rows, no `load_only`) |

Deliberately unchanged: trial responses do NOT gain `billed_user_id` (quotas
spec kept it out of every response builder; adding it would also force a
compact `load_only` addition in `list_tasks_core` — the MissingGreenlet
gotcha). Both computations get billable info without touching responses.

## Changes

1. `oddish/src/oddish/schemas.py` — add the four `billed_*` fields to
   `TaskBrowseItem`, `TaskCostTotals`, and `TaskVersionSummary` (defaults
   0/False).
2. Browse (`tasks_query.py`) — add `TrialModel.billed_user_id` to the trial
   scan select; in the fold, when `billed_user_id` is not None also accumulate
   the `billed_*` aggregate; wire into `TaskBrowseItem`.
3. Task detail (`task_detail.py`) — `get_task_detail_core` builds
   `billed_trial_ids = {t.id for t in all_trial_models if t.billed_user_id}`
   and passes it to `_aggregate_task_detail_rollups`, which fills the
   `billed_*` fields on totals and per-version buckets (trial ids are unique;
   response and model lists are parallel).
4. Frontend — `frontend/src/lib/types.ts` mirrors the new fields.
   `task-card.tsx`: keep the Cost cell, add a muted second line
   `spent {billed}` when priced. `task-detail-client.tsx`: rename the total
   stat to "Total cost (all versions)", add a "Billed spend" stat card reusing
   `CostBadge`; version card unchanged (fields exist if wanted later).
5. Tests — extend `oddish/tests/test_task_detail_endpoint.py` (the fold is
   deliberately unit-testable): billable + NULL-billed trials on one task;
   assert total includes both, `billed_*` only the billable one, and
   `billed_cost_usd <= cost_usd`. Extend a browse test the same way if one
   covers cost fields.

## Scope notes / accepted gaps

- Superseded (retried-away) billable attempts are excluded from BOTH numbers
  (the existing trial scope). True settled spend per quota counts them, so
  "Billed spend" can slightly undercount real money; accepted to keep
  spend <= total and the trial set identical for both numbers.
- Billed spend uses the same native-else-estimate pricing as the indicator and
  the admin dashboard, not the quota page's settled floor
  (`unpriced_trial_cost_usd`); the two pages answer different questions.
- Experiment-page header cost (client-side `accumulateTrial`) stays total-only;
  splitting it would require exposing billable info on trial responses.
- Tasks multi-select "Total:" footer keeps summing total cost.

## Delivery

New worktree + branch off `origin/main` (independent of #597's files), e.g.
`~/worktrees/oddish-task-cost-spend`, branch `feat/task-cost-billed-spend`,
sibling PR. This plan doc moves into that PR.
