# Prepared dashboard rows and file directories

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
after 24 hours to reconcile discrepancies. Eligible pending revisions take priority
over clean daily reconciliations; failed rows retain their retry delay. A never-built row explicitly reports
`summary_pending`, displayed as “Preparing…” rather than a fabricated count.

SWR, the browser's shared request cache, owns experiment lists. Keys include the
signed-in user, organization, and all URL filters. Org/Mine uses browser history
and the existing dashboard JSON endpoint. Search input remains an editable draft;
applied filters belong to the URL. Deleting an experiment clears all cached list
variants for the current user and organization and refreshes the mounted list;
other variants fetch once on their next visit. Request deduplication lasts two
seconds so five-second polls for pending summaries and active trials can run.
Completed lists retain their thirty-second polling interval.

## Prepared file APIs

`file_indexes` identifies a published storage source and its revision.
`file_entries` stores file and directory paths, parents, sizes, and artifact
classification. Partial indexes support pending jobs and artifact-only reads;
a directory index supports `(source, parent, path)` pagination. One SQL statement
reads the source revision and up to `limit + 1` entries per requested directory
from the same database snapshot. No storage operation runs on this path.

Task expansion publishes its index in the same transaction that selects the
completed immutable expanded directory. Trial workers capture metadata during
upload, resolve Harbor's authoritative attempt/child with the existing resolver,
and publish only after uploads finish. A failed metadata publication leaves the
uploaded bytes intact; the durable indexing queue retries it. Source-pointer
triggers enqueue historical/new task versions and trial attempts. Historical
archive-only versions enter the existing expansion queue. Versionless tasks have
task-owned indexes keyed by task ID and their existing storage pointer. Migration
`legacy_file_index_001` queues existing sources and installs a task-pointer
trigger for later changes; a null pointer retains the canonical `tasks/<id>/`
fallback. Background indexing uses the existing archive/directory reader and
keeps archive-member keys as `<archive>#<path>`. It never creates version rows or
changes historical task/trial version links. The indexing worker drains
at most eight records per cycle, backs off failures for five minutes, and stops
starting work after 30 seconds. It never repeatedly scans the complete trial table
to discover missing indexes. Index publication and replacement are transactional;
published old task sources and attempts remain available to authorized readers.

The web app requests `indexed=true`, metadata-only options and 100 entries per
folder. Artifact requests use `artifacts=true`; they do not enumerate logs or
other directories. Pending index jobs return HTTP 503 with `Retry-After: 2`, and
the browser retries preparation. An absent index returns HTTP 404; preparation
is reported only when a durable job exists. Trial preview requests include the displayed attempt,
index revision, and `max_bytes=102400`. Stale attempt/revision requests return
HTTP 409. The storage reader sends an S3 byte range and also bounds the body read.
Full-file loading is a separate explicit action. The authenticated and public
Next.js trial-file proxies forward these query parameters.

SWR, the browser's shared request cache, owns experiment lists, folder pages and
previews. Keys include the signed-in user/organization (or the public-token URL),
filters, task version/content hash or trial attempt/index revision. Org/Mine uses
browser history and the existing dashboard JSON endpoint. The Files and Artifacts
views use `useTaskFileTree` for inventory freshness and pagination, with separate
bounded requests for directory pages and artifact-only pages. Trial panes refresh
on activation, including when a retained pane reopens after an empty listing.
Cached data stays visible during that request. A matching revision preserves
loaded pages; a replacement discards every old page. A preview or continuation
request returning HTTP 409 refreshes the inventory, and the new revision selects
the next preview request. File bodies start only after that revision is known.
Both views share one trial-preview fetcher and key; full-file contents also live
under that key, so a new revision cannot retain an old full download. Binary and
full-file URLs carry the same attempt and revision. Visited file panes retain
selection while hidden and pass their active state to the hook. Hover/focus starts loading drawer modules. Search input remains an
editable draft; applied filters belong to the URL.

## Deployment and alerts

Apply core migrations through `archive_index_001` before deploying
the backend and frontend; `prepared_reads_001` follows `merge_finding_tiers_001`. Hosted workers run every five seconds in `API_REGION`,
the API's configured region. Each worker has `max_containers=1`; advisory locks
also protect standalone deployments. The standalone worker pool starts and
cancels both maintenance loops with its existing lifecycle.

The migration seeds pending work; backfill runs automatically. Until a source's
first build completes, the UI shows preparation rather than performing an
unbounded fallback scan. Check backfill progress before promoting staging to
production. Large initial backlogs can take longer than the normal refresh
interval. Database statistics must remain current; the maintenance timeout/backoff
also covers poor PostgreSQL plans, and does not make arbitrarily large aggregate
rebuilds instantaneous.

The independent dashboard precompute schedule records `worker.prepared_read_health`
with pending-summary count, oldest pending-summary age, and pending-file-index
count. Summary rebuild failures and indexing failures emit error logs. The
Logfire alert catalog includes rules for summary lag above 60 seconds and
prepared-request p95 above 750 ms with at least 20 observations. These are
repository-side alert definitions; enabling notification destinations in Logfire
is a deployment operation, not performed by this draft PR.

## Verification

`.github/workflows/prepared-reads.yml` runs migrations, PostgreSQL correctness and
scale tests, and Chromium tests of real React components with controlled API
responses. Checks enforce transaction rollback, revision races, durable retries,
daily reconciliation, bounded folder/artifact pages, zero storage operations on
prepared listings, attempt/revision isolation, and browser cache reuse. Migration
tests cover both an existing schema and the current-model bootstrap, legacy
archive/directory indexing, preserved historical links, and task-pointer changes.
Browser tests cover late publication after an empty inventory, retained-tab
activation, 409 recovery, revision changes after full downloads, and exactly one
preview body request on a cold trial deep link. The original task-file tests also
preserve direct early body reads, URL line selection, and version/account isolation.

The local PostgreSQL 17 scale fixture uses 1 and 10,000 trials and updates planner
statistics after bulk seeding. Both use exactly two SQL statements per dashboard
read and no trial-history SELECT. In the recorded run, prepared-read medians were
1.17/1.12 ms and full aggregate rebuilds took 16.36/41.77 ms respectively. Local
loopback measurements exclude production authentication, proxies, network travel,
and rendering; they are not production latency predictions. Folder tests compare
1 versus 10,000 unrelated logs and still return at most 100 artifacts per page.

## Archive directories and temporary URLs

Apply migrations through `archive_index_001`. It merges the existing file-index
chain with `prepared_status_001`, including for databases already on
`legacy_file_index_001`, and invalidates unexpanded archive indexes on overwrite.

When extraction exceeds `tasks_expand_max_bytes`, the existing `TASK_EXPAND` job
downloads to temporary disk in 1 MiB chunks and scans the compressed tar stream
for member names and sizes. It publishes through `publish_file_index` using the
same 500-entry batches. The job retains the existing heartbeat and retry policy;
no additional scheduler or foreground storage scan is introduced. The version
lock and captured hash reject publication after an in-place upload. A completed
index ends automatic re-enqueueing; `expanded_at` stays unset so an operator can
still request extraction after changing the size limit. Individual file reads
retain the archive reader.

Signed task-file responses include `expires_at` in Unix seconds. Task previews
retain text and unexpired URLs in SWR. An expired URL is hidden until the same
SWR fetcher renews it, with a 30-second margin before server expiry. A ref guards
the cached response against duplicate renewal effects. Image errors can request
one renewal per source; renewal itself does not reset that allowance. Both task
file proxies return `no-store` for signed URLs and preserve text caching.
Browser regressions advance the clock by 16 minutes and check a second signing
request, no request for the expired image URL, one cached text read, and no
additional directory reads. Separate tests cover one recoverable image failure
and repeated failures stopping after one renewal.
