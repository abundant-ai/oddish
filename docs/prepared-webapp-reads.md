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

Prepared file routes require `indexed=true` and metadata-only options. They
return bounded directory pages without storage reads. Pending jobs return HTTP
503 with `Retry-After: 2`; absent jobs return 404. Trial previews accept attempt,
revision, and a byte bound. Stale attempts/revisions return 409, and storage reads
enforce their byte bound even across partial network reads. Existing directory
batches, previews, and streaming remain available to the current browser.

Apply `file_index_001` and `legacy_file_index_001` after `prepared_reads_001`.
The hosted file maintainer runs every five seconds in the API region; standalone
polling workers start and cancel the same loop. Independent health sampling also
reports pending file indexes. Inspect backfill progress before browser adoption.
File tests cover bounded reads/inserts, rollback, source-pointer publication,
stale attempts/revisions, upload/backfill races, versionless sources, migrations,
and the existing batch/preview/streaming API during this intermediate release.

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

Signed task-file responses include `expires_at` in Unix seconds so browser
preview caches can retain text while renewing temporary storage access.
