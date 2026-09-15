# Prepared dashboard rows

The dashboard reads `experiment_summaries`, which stores the last completed
counts, effective-version scores, primary task identity, and latest runner for
one experiment. `rebuild_dashboard_experiments` in the core dashboard module
owns the existing calculation. Only background maintenance runs it. Ordinary
Org/Mine and status-filter requests select prepared rows before pagination and
hydrate tags and referenced member names. Explicit trial-metric and author
search predicates retain their existing eligibility rules and can query trials.
Cancellation and delivery decisions continue to read authoritative domain rows.

Database triggers on experiments, tasks, versions, trials, and both membership
tables increment the affected experiment's revision in the same transaction.
`last_dirty_txid` coalesces all changes to one experiment in one PostgreSQL
transaction into one revision increment; a bulk import does not rewrite its
summary marker for every trial. Heartbeat/token-only updates are excluded.
The pending marker survives worker restarts. The maintainer groups up to 32
candidates by organization and reuses the same aggregate calculation. A
PostgreSQL advisory lock permits only one maintainer, including across hosted
and standalone processes. It holds no summary-row lock while calculating.
Publication and retry scheduling lock available rows with `FOR UPDATE SKIP LOCKED`;
writer-owned rows retain their pending work and retry time for the next pass.
Publication acknowledges only the revision captured before calculation; a
concurrent change remains pending. Failures preserve the previous payload and
retry after 30 seconds. Each group has a separate database session and a
20-second wall-clock limit, so cancellation cannot invalidate the publication
transaction. Statements have a 15-second limit. Clean rows are also rebuilt
after 24 hours to reconcile discrepancies. A never-built row explicitly reports
`summary_pending`, displayed as “Preparing…” rather than a fabricated count.

SWR, the browser's shared request cache, owns experiment lists. Keys include the
signed-in user, organization, and all URL filters. Org/Mine uses browser history
and the existing dashboard JSON endpoint. Search input remains an editable draft;
applied filters belong to the URL.

## Deployment and alerts

Apply migrations through `prepared_status_001` before deployment. Its task trigger
includes execution status, so status-only QA completion invalidates summaries even
when verdict fields do not change. It also upgrades already-installed triggers.
Hosted summary maintenance runs every five seconds in `API_REGION` with one
container. The standalone polling worker starts and cancels the same maintenance
loop with its lifecycle. Migration seeds pending summaries; backfill is automatic.
First builds display preparation and previous completed summaries remain readable.

The independent dashboard precompute schedule records `worker.prepared_read_health`
with pending-summary count and oldest pending-summary age. Repository-side Logfire
alert definitions cover summary lag above 60 seconds and dashboard request p95
above 750 ms with at least 20 observations. Notification destinations are enabled
separately during rollout. Preview database preparation redeploys existing backend
containers so they receive the newly rotated database password.

## Verification

`.github/workflows/prepared-reads.yml` runs migrations, PostgreSQL correctness and
scale tests, and browser tests with real React components and controlled responses.
Tests cover transactional invalidation and rollback, changes during a rebuild,
coalescing, retries, daily reconciliation, collection membership and runner rules,
ownerless Mine results, and two SQL statements with 1 versus 10,000 trials.
The browser test covers Org/Mine request reuse, search, and browser Back behavior.
