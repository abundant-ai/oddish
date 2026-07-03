# S1 — Cost Completeness (REVERTED)

> **Status: reverted.** The cost-synthesis machinery this slice originally added
> (the `apply_settled_cost` chokepoint, the native→estimate→`$0.50`-floor
> settlement chain, cross-attempt accumulation, and the `cost_settled_attempt`
> once-only gate) has been removed. This doc records what S1 *was* and what the
> code does now.

## Current behavior

`trials.cost_usd` is the **provider-reported** native cost and nothing else. It
stays `NULL` when the provider reports no cost — there is **no** synthesized
floor or token-based estimate written at settlement, and a retried trial shows
its **last** attempt's cost (no accumulation). This is the pre-S1 last-value
assignment: terminal writers set `trial.cost_usd = outcome.cost_usd` (or leave
it `NULL`).

Consequences (accepted):

- The daily-spend `SUM(cost_usd)` skips `NULL` rows, so a cancelled or
  no-native-cost trial contributes `0` to *settled* spend, and a
  start-then-cancel loop can undercount.
- Enforcement is not fully bypassable in real time, because in-flight work is
  still charged via the **reservation** side (below), not the settled sum.

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
provider figure; quota enforcement relies on the in-flight reservation for
real-time protection and accepts settled-sum undercounting for cancelled work.
