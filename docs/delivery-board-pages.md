# Delivery board pages

The dashboard reads `GET /deliveries/{id}/view` instead of downloading the complete
delivery and filtering it in the browser. A response carries 10, 25 (default), 50,
or 100 rows; only the expanded task receives full finding bodies. The original
`GET /deliveries/{id}` still returns the complete board for CLI callers and other
integrations. Both use the same approval calculator in `oddish/core/deliveries.py`.
Hosted and standalone servers expose both new routes. No database migration is
required; deploy the backend routes before the frontend that consumes them.

## Database work

Membership, successful rollout counts, the latest completed verdict's version,
and highest existing version now come from one statement instead of four. Each
aggregate is grouped to one row per task/version before it joins membership, so
trial and version joins cannot multiply the counts. Membership ordering includes
the member ID as a final tie breaker. Soft-deleted trials remain excluded from
rollout/verdict eligibility; deleted member tasks remain visible as blockers.

The page read still evaluates required facts for the entire delivery to produce
accurate totals and filtering. It excludes unused task/version columns and asks
PostgreSQL for trial finding identity fields instead of full descriptions and
metadata. The shared collector preserves retained reports, legacy tiers, deleted
and superseded reporting trials, defect IDs, and acknowledgment decisions.
Full trial evidence is loaded only for the expanded version, after pagination.
The expanded row is copied for the response; sibling finding identities and the
input compact board remain unchanged.

This is not a constant-time database read: pre-trial audit JSON is still needed
for the existing audit fingerprint, retained findings and global review facts
still scale with the inventory, and compact ID lists contain all members. A large
single expanded finding can also produce a large page response. Production
latency must be remeasured; synthetic payload savings are not a production speed
estimate.

Single-task sign-off computes only that member's evidence. Finalization and
scheduled progress recording still check every member. Reads do not start QA,
write acknowledgments, or persist readiness. Frozen deliveries paginate their
saved snapshot without recomputing current task evidence.

## URL and response contracts

| Parameter | Behavior |
| --- | --- |
| `page` | One-based positive safe integer; out-of-range pages clamp to the last page. |
| `per_page` | 10, 25, 50, or 100. Direct API requests with other values return 422. |
| `filter` | `all`, `needs_work`, `qa_incomplete`, `awaiting_signoff`, `ready`; legacy `outstanding` and `blocked` remain accepted. |
| `issue` | `all`, `instructions`, `verifier`, `environment`, `evidence`, or `qa_execution`. |
| `owner` | `all`, `mine`, `unassigned`, or a user ID. `mine` uses the authenticated viewer. |
| `group` | `none`, `owner`, `state`, or `issue`; case-insensitive server label ordering, stable member order for ties. |
| `task` | Task ID, or a legacy task name. ID wins. A matching task stays visible outside filters and selects its containing page. |
| `panels` | Browser disclosure state; preserved in links and history without changing the API request/cache key. |

The frontend retains unrelated parameters, including agent-supplied parameters,
and the URL fragment when controls update the view. Its existing parser
normalizes unsupported filter/page values before making a request. Task detail
links retain their version, finding, file, and line parameters. Back/Forward and
shared links restore filters, grouping, expanded task, and disclosures.

`DeliveryPageResponse` adds a server-computed `state` to each row. `task_count`,
`ready_task_count`, `ready`, delivery checks and progress history still describe
the entire delivery. `owner_counts` is scoped only to the selected owner; state
and issue filters narrow the task queue without changing those summary counts.
`total` counts the matching queue, including an explicitly focused task outside
filters. `focus_task_id` and `focus_outside_filters` describe that exception.
`member_task_ids` supports the Add tasks exclusion list. `matching_task_ids`
contains delivery-member IDs and keeps selection scoped to the current view.

`GET /deliveries/{id}/selection` accepts the same query and returns compact
records for every matching member, ignoring pagination. Select all therefore
includes tasks beyond the rendered page. Each record contains the viewed
`version_id`; bulk sign-off confirms those exact versions and the existing write
endpoint rejects a changed default version. The browser never silently replaces
a selected version with a newer version returned by polling.

Hosted reads require the same task scope and organization approval as the full
board. Approval and data reads share one database session; a revoked organization
is denied on the next request even if its identity is cached.

## Browser refresh and prefetch

The server seeds only the exact normalized page query. Cache ownership includes
user, organization and delivery. Matching server data avoids an immediate
duplicate browser request. A cached active delivery page at least 15 seconds old
refreshes on activation; active boards keep the existing 15-second poll. Frozen
boards do not poll. Navigation keeps the previous rows visible with an updating
status while the requested page loads, and disables row mutations in that interval.

A write marks all cached pages stale and invalidates older in-flight requests,
then refreshes the mounted view. It retains displayed data if that refresh fails
and exposes the existing stale-data warning and retry. Each acknowledged finding
shows Saving through its write and Updating through the subsequent read; unrelated
actions can proceed after its write finishes.

A failed page/filter request ends the loading indicator and keeps the displayed
rows usable. Their grouping, owner summary and Select all request use the last
successful response's query. The URL and filter controls retain the requested
view so Retry can load it without losing agent parameters. No extra request or
separate React state is needed to maintain this distinction.

History prefetch begins after 150 ms of row hover or keyboard focus, with at most
two active speculative reads. Opening history shares its pending request.
The delivery poll remains the sole timer and refreshes only expanded history.

## Validation

`oddish/tests/test_delivery_view.py` compares real PostgreSQL page results with
the complete board using 32 tasks and eleven large findings. It checks all pages,
read-only SQL with at most eight core statements on unexpanded pages, matching
checks and readiness, and each 10-row payload below one tenth of the full board
payload. It also checks focused evidence outside filters, selection across pages,
single-task sign-off scope, whole-delivery finalization, frozen snapshots and
organization isolation. These are fixture bounds, not measured staging timings.

`backend/tests/test_delivery_page_routes.py` exercises actual HTTP query parsing
and cached-identity approval revocation against PostgreSQL. The existing delivery,
QA and progress suites continue to test shared approval behavior. The CLI CI job
runs these suites after initializing its disposable database.

The dedicated Playwright suite covers polling, failed refresh/retry, stale
responses after mutations, account isolation, shared URLs and Back/Forward,
focused tasks outside filters, exact bulk versions across pages, acknowledgment
progress, intent prefetch and desktop/mobile presentation. The dashboard CI job
runs it with the URL/state unit tests. From `frontend`, use:

```sh
node --experimental-strip-types --test tests/delivery-view.test.ts tests/delivery-qa.test.ts
pnpm exec playwright test -c playwright.delivery.config.ts
pnpm exec tsc --noEmit
pnpm exec tsc -p e2e/tsconfig.json --noEmit
```

Use Node 22.18+ for native TypeScript unit-test execution. Point Python tests only
at an isolated database with both core and hosted schemas initialized; delivery
fixtures roll back their transactions. Do not use a staging database for tests.
