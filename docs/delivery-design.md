# Design: Deliveries

Status: phases 1-2 and QA history implemented (tables, board, manual
checks, finalize, hosted routes, dashboard pages). Sharing (phase 3) and
delivery-rubric QA (phase 5) are not built yet.

## The problem

We ship batches of tasks to customers. Today there is no object in the system
that represents "this batch, for this customer." So the shipping workflow is
manual: scroll experiments, check which tasks got QA'd, notice (or miss) that
a task was edited after QA ran, track doneness in a spreadsheet, and build the
customer-facing QA story by hand.

PR #1388 ("experiment QA sharing") attacked the last step — a curated,
publishable QA report — but anchored it to an experiment and froze it on
publish. That is the wrong grain. An experiment is an execution grouping; a
batch for a customer is a set of *tasks*, and it must stay live while the
tasks are still moving.

## What a delivery is

A **delivery** is a checklist that answers one question:
**can we ship this set of tasks?**

- It holds a list of tasks. You add tasks over time.
- Each task must pass a list of checks. Most are computed automatically from
  data we already have; some are boxes a human ticks.
- Checks always evaluate against the task's **current default version**
  (`tasks.current_version_id`). If someone edits a task, the version-scoped
  checks flip red on their own until the new version is re-validated.
- **Ready** = every check green for every task, plus any delivery-level checks.
- When it's ready, an admin **finalizes** it: task versions are pinned and a
  snapshot is stored. That snapshot is the permanent record of what shipped.
- A share link shows the customer the same checklist and the QA behind it,
  minus internal fields.

No customer table for now. A delivery carries a free-text `customer_name` and
an optional QA rubric of its own. If we later want real customer entities, we
add a `customers` table and a nullable `customer_id`; nothing here blocks that.

## Data we already have (nothing new needed to compute readiness)

| Signal | Where it lives | Keyed by |
|---|---|---|
| Pre-trial audit result | `task_versions.pre_trial` / `pre_trial_status` | task version |
| Rollouts | `trials` (`kind='agent'`, non-probe, non-superseded — `EligibleTrialScope`) | `trials.task_version_id` |
| Verdict | `tasks.verdict` (+ `verdict_status`), produced by the `qa`-kind trial | current version |
| Defects (action items) | trial `analysis` / verdict action items (`tier` must_fix / should_fix / optional) | trial → version |
| QA disagreement votes | `feedback` table | verdict / action item |
| Past QA runs | old `qa` / `audit`-kind trials + their stored artifacts | `trials.task_version_id` |

Key existing nuance: `tasks.current_version_id` is a human-chosen default, not
necessarily the numerically newest version (AGENTS.md). Deliveries track the
default on purpose — it is the blessed version. The UI should flag when a
newer non-default version exists.

## Schema

Four tables. All readiness state is **derived at read time**; we store only
membership, human ticks, config, and finalize snapshots.

```
deliveries
  id, org_id, name, customer_name (text, nullable)
  description (text, nullable)
  status: 'active' | 'finalized'        -- one-way; see Finalize
  qa_config (jsonb, nullable)           -- optional delivery-specific QA rubric
                                        -- (preset/skills refs); null = generic QA
  check_config (jsonb)                  -- which built-in checks apply + params
                                        -- (e.g. min_trials, min_agents) and the
                                        -- list of manual check definitions
  is_public (bool), public_token (text, nullable)
  finalized_at, finalized_by_user_id (nullable)
  created_by_user_id, timestamps, deleted_at

delivery_tasks
  id, delivery_id FK, task_id FK
  pinned_version_id FK task_versions (nullable)  -- null while active;
                                                 -- stamped at finalize
  customer_note (text), internal_note (text)
  is_visible (bool, default true)                -- hide a task from share view
  sort_order
  UNIQUE (delivery_id, task_id)

delivery_manual_checks
  id, delivery_id FK
  delivery_task_id FK (nullable)        -- null = delivery-level check
  check_key (text)                      -- matches a manual check defined in
                                        -- deliveries.check_config
  task_version_id FK (nullable)         -- the version the tick attests to
  checked_by_user_id, checked_at, note
  UNIQUE (delivery_id, delivery_task_id, check_key)

delivery_snapshots                      -- append-only, written at finalize
  id, delivery_id FK
  snapshot (jsonb)                      -- full readiness board + QA history,
                                        -- customer-safe fields only
  scope (jsonb)                         -- [{task_id, task_version_id}]
  created_by_user_id, created_at
```

Manual-tick reset falls out of the schema: a tick stores the
`task_version_id` it attested to, and the readiness computation only honors a
tick whose version matches the task's current default. Edit the task → the
tick is stale → the row goes red. No triggers, no cleanup job.

## Checks

Built-in automated checks (each toggleable / parameterized in `check_config`):

1. **pre_trial_passed** — audit on the current version exists and passed.
2. **min_rollouts** — ≥ N eligible trials on the current version across ≥ M
   agents (defaults: the existing verdict evidence bar, 5 trials / 3 agents).
3. **verdict_ok** — verdict exists for the current version, status not FAILED,
   classification acceptable (no oracle/nop-style violations).
4. **no_must_fix** — no open `must_fix` action items against the current
   version. (`should_fix` shows as a warning, not a blocker, by default.)
5. **no_open_disagreements** *(optional, off by default)* — no unresolved
   "disagree" feedback votes on the verdict/items.
6. **delivery_qa_ok** *(only when `qa_config` is set)* — a QA run using the
   delivery's rubric exists for the current version and passed. Runs are
   kicked off manually from the delivery page (they cost money); the check
   reads "missing/stale for vN" until someone runs it. Mechanically this
   reuses the QA replay machinery (`qa_eval`) with the delivery's config.

Manual checks: defined in `check_config` as `{key, label, scope}` where scope
is `task` or `delivery`. Task-scoped ticks are version-bound as above;
delivery-scoped ticks (e.g. "customer confirmed scope") are not.

Readiness for the delivery = AND over all applicable checks over all visible
tasks, plus delivery-level checks. The API returns the full matrix, not just
the boolean, so the UI can show *why* it's red.

## QA history

We want to show, per task, the QA trail over time — audits run, defects
found, fixes shipped, re-QA passed — and share it with the customer. All of
it is reconstructible from existing rows; nothing new is stored:

- `task_versions` gives the version timeline (created_at, message, and each
  version's `pre_trial` audit result).
- `qa`-kind and `audit`-kind trials for the task give every QA run: which
  version it graded (`trials.task_version_id`), when, its status, and its
  imported analysis. `tasks.verdict` only holds the latest verdict, but the
  historical QA trials and their artifacts persist, so past verdicts are
  readable per run.
- Trial analyses give the defects each run found, with tier and dimension.

New core helper: `get_task_qa_history(task_id)` → ordered list of
`{version, version_message, pre_trial, qa_runs: [{when, status, verdict_class,
defects}]}`. Two consumers:

1. Internal: a "QA history" panel on the task row inside the delivery page.
2. Public: the same structure, field-filtered (see next section), on the
   share page. This is the "here's the rigor behind what you're getting"
   story, generated instead of hand-written: *v1 audited → 2 must-fix found →
   v2 fixed them → re-QA green*.

Since older QA artifacts live in object storage, the public path should read
through the snapshot at finalize time (history is embedded in
`delivery_snapshots.snapshot`) and compute lazily with caching while the
delivery is active.

## Sharing (exposing QA to the customer)

Follow the existing public-view rules exactly (AGENTS.md):

- Single 256-bit token on the delivery; routes live under
  `/public/deliveries/{token}/...` and every sub-route verifies membership.
  No ID-only public routes.
- Unpublish clears the token (old links die); republish mints a new one.
- Field filtering at the query layer: internal notes, raw QA payloads,
  errors, hidden tasks/items, and anything probe-related never enter the
  public payload. Reuse #1388's filtered-payload approach.
- Evidence links stay inside the share page; never link out to authed routes.

Customer sees: the checklist board (N/M ready, per-task check status), each
task's customer note, curated defect list with resolutions, and the QA
history trail. The live board is visible while the delivery is active;
after finalize the share page serves the frozen snapshot.

## Finalize

- Allowed only when the board is fully green (server-enforced), by an admin.
- Stamps `pinned_version_id` on every `delivery_tasks` row, writes one
  `delivery_snapshots` row (customer-safe board + QA history + scope), sets
  `status='finalized'`, `finalized_at/by`.
- Finalized deliveries are read-only. Follow-up work is a new delivery
  (cheap to create; can be cloned from the old one). This keeps "what did we
  ship in August" a one-row answer and avoids re-open semantics entirely.
- Deleting a task or unpublishing while public follows #1388's revocation
  pattern: scope change on an *active* public delivery revokes the token;
  finalized snapshots are unaffected (they're copies, not references).

## API surface

Core (oddish/core, mirrored as hosted routes under `backend/api`):

```
POST   /deliveries                          create (admin)
GET    /deliveries                          list
GET    /deliveries/{id}                     board: tasks × checks, computed
PATCH  /deliveries/{id}                     name/notes/config (optimistic lock)
DELETE /deliveries/{id}                     soft delete
POST   /deliveries/{id}/tasks               add tasks
DELETE /deliveries/{id}/tasks/{task_id}     remove
PATCH  /deliveries/{id}/tasks/{task_id}     notes / visibility / order
PUT    /deliveries/{id}/checks/{key}        tick/untick a manual check
POST   /deliveries/{id}/qa-run              kick delivery-rubric QA (per task
                                            or all stale)
POST   /deliveries/{id}/publish             mint token
POST   /deliveries/{id}/unpublish           revoke token
POST   /deliveries/{id}/finalize            pin + snapshot (must be green)
GET    /tasks/{task_id}/qa-history          internal QA history
GET    /public/deliveries/{token}           filtered board (live or snapshot)
GET    /public/deliveries/{token}/tasks/{task_id}   filtered detail + history
```

Mutations are admin-only, with optimistic locking (`expected_version`) as in
#1388.

## Frontend

- `/(app)/deliveries` — list, with ready-count badges.
- `/(app)/deliveries/[id]` — the board: one row per task, one column per
  check, green/red/warning cells, aggregate header ("18/20 ready"), buttons:
  add tasks, run delivery QA, publish, finalize. Task row expands into QA
  history + notes editor.
- `/share/delivery/[token]` — customer view of the same board, filtered.

## What we reuse from PR #1388 (unmerged, branch `meji/experiment-qa`)

Take: the snapshot-on-publish pattern (`qa_report_publications` →
`delivery_snapshots`), token + revocation discipline, field-filtered public
payloads, optimistic locking, the curation idea (customer note / internal
note / is_visible per item), and much of the public-report frontend.

Drop: the experiment anchor, the dual-token scheme (a delivery has its own
single token; it is not nested under an experiment share), and
publish-as-the-product (here the live board is the product; the snapshot is
the finalize record).

Recommendation: don't land #1388 as-is and migrate later — mine it. The PR is
already `dirty` against staging.

## Defaults chosen (flag in review if wrong)

1. Delivery-rubric QA runs are **manual**, surfaced as a stale/missing red
   check — not auto-triggered on task edits (cost control).
2. Human ticks on task-scoped checks **reset on version change** (via
   version-bound ticks).
3. Customers see the **live** board while active, the **snapshot** after
   finalize.
4. Deliveries track `tasks.current_version_id` (the blessed default), not
   max(version); UI warns when they differ.
5. Finalized = immutable; follow-ups are a new delivery.

## Build order

1. **Tables + board (read-only).** `deliveries`, `delivery_tasks`, board
   computation from existing signals, list/detail pages. Immediately usable
   as an internal tracker.
2. **Checks config + manual ticks + finalize.** `check_config`,
   `delivery_manual_checks`, `delivery_snapshots`, green-gate enforcement.
3. **Sharing.** Public routes + share page + revocation wiring.
4. **QA history.** `get_task_qa_history`, internal panel, public trail,
   history embedded in snapshots.
5. **Delivery-rubric QA.** `qa_config`, `/qa-run` via the QA replay
   machinery, `delivery_qa_ok` check. (This is the per-customer QA story,
   attached to the delivery until a customer entity exists.)

Phases 1–3 replace the spreadsheet. 4 and 5 are the customer-facing upgrades.
