# Task-file latency verification

The September 8 staging recording contained eight uncached file reads with a
2.23-second median and a 5.30-second maximum. The file-preparation worker failed
on `DeleteObjects` in staging and production. The recorded storage error omitted
its HTTP status and provider details; the underlying provider cause remains unknown.

This change removes deletion from successful publication, rather than guessing at
that provider failure. Files are uploaded once into a unique directory; a completed
manifest is published through the existing version row. Published text reads use one
storage GET. A missing/skipped member retains archive fallback. Previously published
directories are retained so concurrent readers and signed URLs remain usable.
Old-layout files remain readable during rollout and rollback; an older API can fall
back to the canonical archive when it cannot use the new directory. Repeated
publications retain storage copies; this PR does not add a garbage-collection job.

## Measurements

Filter Logfire to the deployed environment and a file request's trace. Its
`backend.request.phases` record includes:

| Field | Meaning |
|---|---|
| `storage.file.source` | `expanded`, `archive`, or `loose` |
| `storage.request_count` | Instrumented SDK operations/pages, excluding internal SDK retries |
| `storage.download_bytes` | Object-body bytes downloaded by the backend |
| `storage.archive_bytes` | Size of the archive used, including cache hits |
| `storage.archive_cache.hit` | Whether this process already held the current archive |
| `storage.file.bytes` | Requested file size when known; truncated individual-object reads may omit it |

The `storage_head`, `storage_get`, `storage_read`, `storage_list`, `storage_delete`,
and `archive_parse` phases appear in request timing alongside authentication and
database phases. GET measures the operation through response headers; body reading
and archive parsing are measured separately. These are cumulative durations, so
concurrent work can exceed wall-clock request duration. Child spans identify object
keys. Error logs include HTTP status, provider error code/message, request ID, retry
count, and delete batch size; they do not dump headers or object contents.

## Staging acceptance checklist

- [ ] Capture an affected task/version/file before retrying preparation. Separate
  browser-cache hits from requests reaching the backend.
- [ ] Retry preparation for that task through the existing organization-scoped
  `/admin/tasks/expand-backfill?task_id=...&limit=1` POST. The limit is a version count;
  inspect which version was selected rather than assuming it is the desired one.
  Do not run an unfiltered backfill for the initial comparison.
- [ ] Confirm the worker succeeded and the version's `expanded_manifest_key` points
  beneath `tasks/<id>/v<N>-expanded/<token>/`. The file response should use an
  individual object and report `storage.file.source=expanded`.
- [ ] Repeat the same small text-file reads. For an existing published member,
  expect one GET and no archive HEAD/download/parse. Record median and slow-tail
  browser durations; no production speedup is claimed from local tests.
- [ ] Read a missing/skipped member and a legacy version. Confirm archive fallback,
  correct content, and one reused archive HEAD when the archive prefix is known.
- [ ] Change versions and overwrite a version in place. Check file selection,
  deep-linked paths/line anchors, and content correctness. Keep old signed URLs
  usable for their existing lifetime.
- [ ] Delay task details relative to a file listing, then reverse their completion
  order. Matching response fingerprints must preserve the listing; differing
  fingerprints must refresh it. Check JSON and streamed listings.
- [ ] Inspect an identity-cache miss. An existing organization/user with no identity
  refresh work should issue only two SELECTs. New users, role changes, and restricted
  analysis keys must continue to work.
- [ ] Review remaining S3 errors using the newly retained provider diagnostics.
  Deletion errors now affect cleanup rather than successful file publication.

Track each comparison with deployment commit, task/version, file, browser-cache
status, API process/region, file source, operation count, downloaded bytes, and
total duration. Compare those same classes before and after rollout. Authentication,
database connection management, and the Next.js proxy still contribute latency;
their measured durations should guide subsequent work.
