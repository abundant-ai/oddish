# Delivery metadata implementation

Started September 16, 2026. Branch: `backfilling-oddish-metadata` (worktree `abundant/oddish/backfilling-oddish-metadata/`, based on staging `edbb9e7e`).

Goal: select tasks for a lab using permanent identity, task characteristics,
recorded delivery history, and evidence for the exact version being reviewed.

## 1. Historical backfill: first implementation

- [x] Locate the September 10 delivery research after the workspace move.
- [x] Create an isolated checkout from current `origin/staging`.
- [x] Inspect existing task IDs, tags, versions, deliveries, and task-declaration work.
- [x] Build an offline import planner that retains original rows and source references.
- [x] Resolve explicit task IDs and recorded aliases; quarantine conflicting IDs
  and name-only matches rather than treating them as confirmed identities.
- [x] Propose category and alias additions without overwriting existing values.
- [x] Preserve customer/batch history without inventing shipped versions,
  program names, customer acceptance, or QA approval.
- [x] Generate stable record IDs so repeated imports can be reconciled.
- [x] Add an organization-scoped, read-only Oddish inventory export.
- [x] Test rename collisions, cross-organization IDs, conflicting categories,
  source changes, missing versions, and repeated/reordered input.
- [x] Run the planner on the saved research and record actual coverage/conflicts.

Verified locally on Python 3.13.7: `Ran 26 tests ... OK`; Ruff reports
`All checks passed!`. Two runs of the real September 10 research retain 17,895
source records with zero added/changed records on replay. There are 3,622 groups
of related names: 1,484 with one unverified source ID, 114 with conflicting IDs,
and 2,024 without an explicit ID. No live inventory was supplied, so no production
ID matches or metadata additions are asserted. The inventory exporter is written;
its live database query is not yet production-verified.

Local results: `/Users/kyle/Desktop/oddish/metadata-backfill-results/20260916/`.
The organization label `8ebde5d0` is the Abundant organization's internal ID
(`DEFAULT_OPERATOR_ORG_ID` in `backend/modal_app.py`), confirmed as the target
organization on September 17.
See [the runbook](delivery-metadata-backfill.md) for commands and output definitions.

## 2. Persist reviewed facts in Oddish

- [x] Add source-backed task aliases, metadata assertions, and historical delivery
  records with database uniqueness constraints; keep them separate from active
  delivery checklists and finalized snapshots. (Migration `delivery_history_001`;
  tables listed in the runbook. Verified September 17 on a core-only local
  database: `6 passed` in `test_task_history_schema.py`, downgrade/upgrade
  round-trip, and identical schema from the fresh-bootstrap path.)
- [x] Decide how the inventory export and the import reach production.
  Decided September 17: the operator runs it from a laptop with the `oddish`
  CLI; permanent tool; `GET /deliveries/task-inventory` (tasks scope),
  `POST /deliveries/history-imports` (admin), `GET /deliveries/history-imports`;
  plan uploaded as a file; no review view for unresolved groups yet. A full
  write measured 0.9 s locally against the API's 600 s request budget, so
  the import is one request, not a background job.
- [ ] Capture a fresh inventory for the Abundant organization (`8ebde5d0`,
  confirmed September 17); resolve ambiguous names against repository paths,
  experiment membership, and source evidence.
- [x] Add a preview/apply operation with organization checks, stale-plan rejection,
  and an import receipt. Replays update the same source records.
  (`oddish.core.ingest.delivery_apply`; verified September 17: `9 passed` in
  `test_delivery_apply.py` on a local database; served by the routes and CLI
  above, covered by `test_delivery_history_api.py` and
  `test_cli_delivery_history.py`. Not yet run against staging or production.)
- [ ] Import unambiguous facts; expose unresolved conflicts and unknown history.
- [x] Retain import changes and historical shipments when a task is retired.
  (Task foreign keys are `ON DELETE RESTRICT`; `delete_task_core` only stamps
  `tasks.deleted_at`. Covered by `test_retiring_a_task_keeps_imported_history`.)

### Rollout order

1. Land the five stacked PRs on `staging` (inventory export, planner, history
   tables, import core, import routes and CLI); the staging deploy runs
   `delivery_history_001`.
2. Against staging (a mirror of production data): `oddish delivery inventory`,
   the planner on the September 10 research, `oddish delivery import-history`
   preview; review the receipt; apply on staging as the rehearsal.
3. Promote `staging` to `main`; repeat inventory, preview, apply on production.

The staging database is rebuilt from production by the staging refresh
script, so whatever the rehearsal applies on staging disappears at the next
refresh and the production rows arrive in its place. The rehearsal is also
the first upload of a plan this size through the hosted API.

## 3. Select tasks for a lab

- [ ] Add task filters for category, domain, language, environment, source, and
  previous recipients. Preserve original classifications during normalization.
- [ ] Capture lab/program requirements with revisions and required evidence.
- [ ] Show meets / fails / unknown for each requirement and exact task version.
- [ ] Exclude prior deliveries to the same lab by default, including across
  programs; permit explicit replacements linked to the earlier submission.
- [ ] Allow reuse across labs while preserving organization access boundaries.
- [ ] Show incomplete history separately from confirmed absence of delivery.

## 4. Review, export, and submission

- [ ] Define reviewer responsibility, required checks, blockers, and exception policy.
- [ ] Bind sign-off to exact contents as well as version ID; in-place content
  replacement must invalidate approval even when the version ID stays the same.
- [ ] Generate the spreadsheet and package from one approved, frozen selection.
- [ ] Record submission attempts and per-task receipts separately from approval.
- [ ] Reconcile partial/uncertain receipts before retrying; reuse attempt IDs.
- [ ] Record customer review, rejection reasons, and linked replacement shipments.
- [ ] Verify a second teammate can complete a delivery using the stored history
  and instructions without relying on the original operator's memory.

## Boundaries

The first implementation prepares local evidence and proposed changes. It does
not infer customer acceptance from upload success, assign historical versions
from current defaults, or treat CSV pass rates as current-version QA approval.
Production application, UI filtering, and shipment execution remain later steps.
