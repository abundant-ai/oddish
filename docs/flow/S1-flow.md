# S1 — Cost Completeness (REVERTED)

> **Status: reverted.** The cost-synthesis machinery this slice originally added
> (the `apply_settled_cost` chokepoint, the native→estimate→`$0.50`-floor
> settlement chain, cross-attempt accumulation, and the `cost_settled_attempt`
> once-only gate) has been removed. This doc records what S1 *was* and what the
> code does now.

## Current behavior

**At the row**, `trials.cost_usd` is the **provider-reported** native cost and
nothing else. It stays `NULL` when the provider reports no cost — there is **no**
synthesized floor or token-based estimate written at settlement, and a retried
trial shows its **last** attempt's cost (no accumulation). This is the pre-S1
last-value assignment: terminal writers set `trial.cost_usd = outcome.cost_usd`
(or leave it `NULL`).

**At read time**, the budget is protected without touching the row: the quota
sums (`sum_cost_usd` / `sum_cost_usd_by_user`, `oddish/core/quotas.py`) score
each trial via `_settled_cost_expr()` =
`COALESCE(cost_usd, CASE WHEN started_at IS NOT NULL THEN $10 ELSE 0 END)`. So:

- priced trial → its real `cost_usd`;
- **unpriced but started** (finished with a `started_at`, e.g. cancel / reap /
  retry-supersede / estimate-only) → `unpriced_trial_cost_usd` (`$10`, env
  `ODDISH_UNPRICED_TRIAL_COST_USD`) — a start-then-cancel loop is **not** free;
- unpriced and **never started** (cancelled while PENDING/QUEUED) → `$0`, since
  it did no billable work;
- a genuine `$0` row → `$0`.

The trial row itself keeps `cost_usd = NULL`; only the SUMs floor it. Known gap:
delete-*while-running* tombstones a row without `finished_at`, so it isn't
counted (would need a settle-on-delete if desired).

## What remains (not part of the revert)

- **In-flight reservation floor.** `pending_trial_reservation_usd`
  (`ODDISH_PENDING_TRIAL_RESERVATION_USD`, default `$0.50`) is used **only** by
  S5 admission: `inflight_reserved_usd` reserves `GREATEST(cost_usd, $0.50)` per
  live trial. It is *not* a settled-cost floor.
- **Display-side estimate.** `estimate_cost_usd(...)` still runs on **read** in
  `core/helpers.py` / `tasks_query.py` to show a cost in the UI when the native
  value is `NULL`; it is never persisted.
- **`cache_write_tokens`** token accounting (model column, `HarborOutcome`
  field, and the read-side estimate's 5th argument) stays.

## Why it was reverted

Cost completeness changed `cost_usd` semantics for **every** deployment
(settled cancelled/failed/no-native-cost trials went from `NULL` → estimate or
`$0.50`), regardless of `quota_mode` — i.e. it did not "ship dark," and it
shifted every `SUM(cost_usd)` dashboard. The MVP keeps `cost_usd` as the raw
provider figure and instead handles the "invisible spend" concern in the **read
layer** (the `$10` unpriced-but-started floor above) plus the in-flight
reservation — so no settlement write ever changes `cost_usd`, yet a
start-then-cancel loop still can't run real compute for free.
